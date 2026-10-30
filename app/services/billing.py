"""
Issuing school fee invoices, taking payments against them, and the gateway seam.

`app.services.finance` works out what a student owes. This module turns that into a document
somebody is asked to pay, and records what arrives.

The one rule that matters: **a DRAFT is a view, an ISSUED invoice is a record.** Regenerating
a draft recomputes it from the current structure, discounts and settings, which is what makes
"preview next year's bills" safe. The moment it is issued the numbers stop moving. A bill that
silently changed after it was sent is not a bill, and a parent holding a printout that no
longer matches the system is an argument the school cannot win.

Payments are appended, never summed from thin air. `amount_paid` is recomputed from the
`payments` list on every write rather than incremented, so a duplicated request cannot drift
the total away from the receipts that justify it.

**The gateway seam.** No provider is wired - the brief is to show fees, charges and taxes now
and integrate later. `create_intent` and `settle_intent` are the two functions an adapter will
need, and they already enforce the guarantee that cannot be retrofitted: an idempotency key,
so a webhook delivered twice credits one payment. Everything provider-specific lives behind
`provider` and `provider_payload`, which is why adding Stripe or Razorpay later is a new file
rather than a migration.
"""

import logging
import threading
import uuid
from datetime import date, datetime, timedelta

from fastapi import HTTPException

from app.core import razorpay
from app.core.enums import (
    OFFLINE_GATEWAY_METHODS, InstalmentStatus, InvoiceStatus, PaymentIntentStatus,
    PaymentMethod, Program, TERMINAL_INTENT_STATES,
)
from app.core.firebase import (
    firestore_fee_invoices, firestore_payment_intents, firestore_users, require_document,
)
from app.services import admissions as admission_service
from app.services import finance
from app.services.finance import money
from app.services.tuition.settings_store import currency_settings, finance_settings

logger = logging.getLogger("billing")

# Statuses whose figures are frozen and must not be silently restated. See the module
# docstring.
#
# CANCELLED is deliberately NOT in here. The document id is derived from (student, year), so
# there is exactly one slot per student per year - and a cancelled invoice occupying it
# permanently would mean cancelling a bill made that student unbillable forever, with the
# error telling the admin to "cancel it and generate a new one" about an invoice that is
# already cancelled. Cancelling is what you do *in order to* reissue; it has to free the slot.
#
# Nothing financial is lost by replacing one: `cancel_invoice` already refuses while any
# payment has been recorded, so a cancelled invoice is by definition one nobody has paid
# against. The cancellation is kept as history on the replacement - see `_cancellation_trail`.
FROZEN_STATUSES = frozenset({
    InvoiceStatus.ISSUED.value,
    InvoiceStatus.PARTIALLY_PAID.value,
    InvoiceStatus.PAID.value,
})


def _cancellation_trail(existing: dict | None) -> list[dict]:
    """
    Carries the record of a replaced cancellation onto the new invoice.

    Regenerating over a cancelled invoice reuses its document id, so without this the fact
    that a bill was raised and voided would disappear. Kept as a short list rather than a
    single field because a fee can legitimately be cancelled and rebuilt more than once in a
    term, and only recording the last one hides the others.
    """
    if not existing or existing.get("status") != InvoiceStatus.CANCELLED.value:
        return list((existing or {}).get("superseded_cancellations") or [])

    trail = list(existing.get("superseded_cancellations") or [])
    trail.append({
        "invoice_number": existing.get("invoice_number"),
        "total_amount": existing.get("total_amount"),
        "currency": existing.get("currency"),
        "cancelled_reason": existing.get("cancelled_reason"),
        "cancelled_by": existing.get("cancelled_by"),
        "cancelled_at": existing.get("updated_at"),
        "superseded_at": _now(),
    })
    # Bounded: this is a breadcrumb for an admin reading the history, not an audit log, and
    # an unbounded list on a hot document eventually becomes a size problem.
    return trail[-10:]


def _now() -> str:
    return datetime.utcnow().isoformat()


def require_invoice(invoice_id) -> dict:
    return require_document(firestore_fee_invoices, invoice_id, "Invoice")


def invoice_id_for(student_id, academic_year_id) -> str:
    """
    A derived document id, one invoice per student per year.

    Derived rather than generated so that regenerating a draft overwrites it instead of
    leaving a second bill behind. Two live invoices for the same year is the state that makes
    every arrears report wrong, and no amount of later de-duplication recovers which one the
    parent actually paid.
    """
    return f"{student_id}:{academic_year_id}"


def _next_invoice_number(config: dict) -> str:
    """
    A human-facing invoice number, `PREFIX-YEAR-0001`.

    Derived from the numbers already issued this year rather than from a stored counter, for
    the same reason `accounts.next_identifier` is: a counter is one more document to keep in
    step, and this collection is read anyway.
    """
    year = datetime.utcnow().year
    stem = f"{config['invoice_prefix']}-{year}-"

    highest = 0
    for invoice in firestore_fee_invoices.list_all():
        number = str(invoice.get("invoice_number") or "")
        if number.startswith(stem):
            tail = number[len(stem):]
            if tail.isdigit():
                highest = max(highest, int(tail))
    return f"{stem}{highest + 1:04d}"


RECEIPT_PREFIX = "RCT"

# Held from choosing a receipt number until the payment carrying it is written, so two
# clerks at two counters cannot both take the next number. Per process: the backend runs as
# one instance, and the numbers are derived from the stored payments, so a restart loses
# nothing.
_receipt_lock = threading.Lock()


def _next_receipt_number(year_name: str | None) -> str:
    """
    The next counter receipt number for a session year, `RCT-2026-27-0001`.

    Numbered per school year because that is how a receipt book runs, and derived from the
    payments already stored - like the invoice number - rather than from a counter document.
    """
    stem = f"{RECEIPT_PREFIX}-{year_name or datetime.utcnow().year}-"
    highest = 0
    for invoice in firestore_fee_invoices.list_all():
        for payment in invoice.get("payments") or []:
            number = str(payment.get("receipt_number") or "")
            if number.startswith(stem):
                tail = number[len(stem):]
                if tail.isdigit():
                    highest = max(highest, int(tail))
    return f"{stem}{highest + 1:04d}"


# ---------------------------------------------------------------------------------------
# Generating
# ---------------------------------------------------------------------------------------

def generate_invoice(student_id, academic_year_id=None, actor_id: int | None = None,
                     program: str = Program.LMS.value, currency: str | None = None) -> dict:
    """
    Builds or rebuilds a student's draft invoice from the current fee rules.

    Refuses to touch an invoice that has been issued. The caller is told to cancel and
    reissue instead, which leaves a trail - the alternative, silently rewriting a bill
    somebody has already been sent, is the failure this whole module is shaped around.
    """
    student = require_document(firestore_users, student_id, "Student")
    config = finance_settings(program)

    if academic_year_id is None:
        year = admission_service.current_year(program)
        if not year:
            raise HTTPException(
                status_code=400,
                detail="No academic year is configured. Create one before billing.",
            )
        academic_year_id = year["id"]

    doc_id = invoice_id_for(student_id, academic_year_id)
    existing = firestore_fee_invoices.get_document(doc_id)

    if existing and existing.get("status") in FROZEN_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Invoice {existing.get('invoice_number') or doc_id} is already "
                f"{existing.get('status')} and its figures are frozen. Cancel it first, then "
                "rebuild it, if the fees have genuinely changed."
            ),
        )

    breakdown = finance.compute_breakdown(
        student, academic_year_id, program, currency=currency
    )

    year_doc = admission_service.firestore_academic_years.get_document(str(academic_year_id)) or {}
    year_start = finance._as_date(year_doc.get("start_date")) or date.today()

    document = {
        "student_id": int(student_id),
        "academic_year_id": int(academic_year_id),
        "structure_id": breakdown["structure_id"],
        "instalment_plan_id": breakdown["instalment_plan_id"],
        "status": InvoiceStatus.DRAFT.value,
        "currency": breakdown["currency"],
        "currency_symbol": breakdown.get("currency_symbol"),
        "base_currency": breakdown.get("base_currency"),
        # Recorded on the draft and frozen at issue. A bill that re-converts at today's rate
        # is a bill whose total changes after it was sent - the exact failure the freeze on
        # issue exists to prevent, extended to the conversion.
        "exchange_rate": breakdown.get("exchange_rate", 1.0),
        "program": str(program).upper(),

        "line_items": breakdown["line_items"],
        "discounts": breakdown["discounts"],
        "instalments": breakdown["instalments"],
        # Preserved across a regeneration: payments already recorded against a draft are
        # money the school has, and rebuilding the bill must not discard the receipts.
        "payments": (existing or {}).get("payments", []),

        "subtotal": breakdown["subtotal"],
        "discount_total": breakdown["discount_total"],
        "taxable_base": breakdown["taxable_base"],
        "tax_total": breakdown["tax_total"],
        "charge_total": breakdown["charge_total"],
        "convenience_total": breakdown["convenience_total"],
        "late_fee_total": (existing or {}).get("late_fee_total", 0.0),
        # Concessions and extra items are given at issue; a rebuilt draft starts clean.
        "extra_total": 0.0,
        "concession_reason": None,
        "total_amount": breakdown["total_amount"],
        "due_date": (year_start + timedelta(days=int(config["invoice_due_days"]))).isoformat(),

        "generated_by": actor_id,
        "created_at": (existing or {}).get("created_at") or _now(),
        "updated_at": _now(),
        # Cleared explicitly: `add_document` merges, so a rebuild over a cancelled invoice
        # would otherwise inherit its number, its cancellation reason and its issued date.
        "invoice_number": None,
        "issued_at": None,
        "cancelled_reason": None,
        "cancelled_by": None,
        "superseded_cancellations": _cancellation_trail(existing),
    }
    document["amount_paid"] = _recompute_paid(document)
    _apply_payments_to_instalments(document)

    firestore_fee_invoices.add_document(doc_id, document)
    document["id"] = doc_id
    logger.info("Generated draft invoice for student %s, year %s: %s %s",
                student_id, academic_year_id, document["currency"], document["total_amount"])
    return document


def _field(obj, name):
    """Reads a request item whether it arrived as a schema object or a plain dict."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _apply_concessions(invoice: dict, concessions, reason: str | None,
                       actor_id: int | None) -> dict:
    """
    Takes a concession off named fee lines, as the office decides it at the moment of issue.

    Each line's amount shrinks, its components shrink in proportion (so a receipt still says
    what was paid for), and the matching line items carry the cut as a discount. One
    "Concession" entry joins the discounts list so the bill shows it as a concession rather
    than as a mysteriously smaller fee.
    """
    instalments = [dict(i) for i in invoice.get("instalments") or []]
    by_label = {i.get("label"): i for i in instalments}
    line_items = [dict(l) for l in invoice.get("line_items") or []]
    item_by_name: dict = {}
    for item in line_items:
        item_by_name.setdefault(item.get("name"), item)

    currency = invoice.get("currency") or ""
    granted = 0.0
    for concession in concessions or []:
        label = _field(concession, "label")
        amount = money(_field(concession, "amount"))
        if amount <= 0:
            continue
        line = by_label.get(label)
        if line is None:
            raise HTTPException(status_code=400, detail=f"There is no fee called '{label}' on this bill.")
        before = money(line.get("amount"))
        if amount > before + 0.009:
            raise HTTPException(
                status_code=400,
                detail=f"The concession on {label} is more than the fee itself ({currency} {before}).",
            )

        components = [dict(c) for c in line.get("components") or []]
        parts = [c for c in components if money(c.get("amount")) > 0]
        remaining = amount
        for index, part in enumerate(parts):
            share = remaining if index == len(parts) - 1 else money(amount * money(part["amount"]) / before)
            remaining = money(remaining - share)
            part["amount"] = money(money(part["amount"]) - share)
            item = item_by_name.get(part.get("name"))
            if item is not None:
                item["discount_amount"] = money(money(item.get("discount_amount")) + share)
                item["net_amount"] = money(money(item.get("net_amount", item.get("amount"))) - share)
        line["components"] = components
        line["amount"] = money(before - amount)
        line["concession_amount"] = money(money(line.get("concession_amount")) + amount)
        granted = money(granted + amount)

    if granted <= 0:
        return invoice
    discounts = list(invoice.get("discounts") or []) + [{
        "rule_id": None,
        "name": f"Concession ({reason.strip()})" if reason and reason.strip() else "Concession",
        "basis": "MANUAL",
        "value_type": "FIXED",
        "value": granted,
        "amount": granted,
        "stacked": True,
    }]
    return {
        **invoice,
        "instalments": instalments,
        "line_items": line_items,
        "discounts": discounts,
        "discount_total": money(money(invoice.get("discount_total")) + granted),
        "total_amount": money(money(invoice.get("total_amount")) - granted),
        "concession_reason": (reason or "").strip() or None,
        "concession_by": actor_id,
    }


def _drop_lines(invoice: dict, labels) -> dict:
    """
    Leaves fee lines off the bill: the office ticks which fees it is issuing, and the rest
    are not asked for at all - not even as a zero line.

    The line items shrink by what the dropped lines carried, so the bill's breakdown still
    adds up to its total. A line with money already paid against it cannot be dropped.
    """
    drop = {str(l) for l in labels or [] if l}
    if not drop:
        return invoice
    instalments = [dict(i) for i in invoice.get("instalments") or []]
    known = {i.get("label") for i in instalments}
    for label in drop - known:
        raise HTTPException(status_code=400, detail=f"There is no fee called '{label}' on this bill.")

    line_items = [dict(l) for l in invoice.get("line_items") or []]
    item_by_name: dict = {}
    for item in line_items:
        item_by_name.setdefault(item.get("name"), item)

    kept, removed = [], 0.0
    for line in instalments:
        if line.get("label") not in drop:
            kept.append(line)
            continue
        if money(line.get("amount_paid")) > 0:
            raise HTTPException(
                status_code=400,
                detail=f"Money has already been paid towards {line.get('label')}, so it stays on the bill.",
            )
        for part in line.get("components") or []:
            item = item_by_name.get(part.get("name"))
            if item is not None:
                share = money(part.get("amount"))
                item["amount"] = money(money(item.get("amount")) - share)
                item["net_amount"] = money(money(item.get("net_amount", item.get("amount"))) - share)
        removed = money(removed + money(line.get("amount")))

    return {
        **invoice,
        "instalments": kept,
        "line_items": [l for l in line_items if money(l.get("net_amount", l.get("amount"))) > 0],
        "subtotal": money(money(invoice.get("subtotal")) - removed),
        "total_amount": money(money(invoice.get("total_amount")) - removed),
        "left_off": sorted(drop),
    }


def _add_extra_lines(invoice: dict, items, actor_id: int | None) -> dict:
    """
    Adds fees that are not in the fee structure - books, uniform, a trip - as lines of their own.

    Each becomes a separate line on the schedule, so it can be ticked and paid on its own and
    reads by its own name on the receipt.
    """
    instalments = [dict(i) for i in invoice.get("instalments") or []]
    taken = {i.get("label") for i in instalments}
    added = 0.0
    for item in items or []:
        name = str(_field(item, "name") or "").strip()
        amount = money(_field(item, "amount"))
        due = _field(item, "due_date")
        if not name or amount <= 0:
            raise HTTPException(status_code=400, detail="Each extra item needs a name and an amount.")
        label, n = name, 2
        while label in taken:
            label, n = f"{name} ({n})", n + 1
        taken.add(label)
        instalments.append({
            "label": label,
            "term": "EXTRA",
            "is_extra": True,
            "due_date": (finance._as_date(due) or date.today()).isoformat(),
            "amount": amount,
            "amount_paid": 0.0,
            "status": InstalmentStatus.PENDING.value,
            "components": [{"name": name, "amount": amount}],
            "added_by": actor_id,
            "added_at": _now(),
        })
        added = money(added + amount)
    if added <= 0:
        return invoice
    return {
        **invoice,
        "instalments": instalments,
        "extra_total": money(money(invoice.get("extra_total")) + added),
        "total_amount": money(money(invoice.get("total_amount")) + added),
    }


def issue_invoice(invoice: dict, actor_id: int | None = None,
                  program: str = Program.LMS.value, concessions=None,
                  concession_reason: str | None = None, extra_items=None,
                  exclude=None) -> dict:
    """
    Freezes an invoice and gives it a number.

    The office can give a concession on any fee line and add extra items (books, uniform) in
    the same step, so what is issued is exactly what the family is asked to pay.

    A zero-total invoice is refused: it means no fee structure matched, and sending a parent
    a bill for nothing is a support call rather than a courtesy.
    """
    if invoice.get("status") in FROZEN_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"This invoice is already {invoice.get('status')}.",
        )
    invoice = _drop_lines(invoice, exclude)
    invoice = _apply_concessions(invoice, concessions, concession_reason, actor_id)
    invoice = _add_extra_lines(invoice, extra_items, actor_id)
    if money(invoice.get("total_amount")) <= 0:
        raise HTTPException(
            status_code=400,
            detail="This invoice totals zero - no fee structure applies to the student. "
                   "Set up a structure for their class or category first.",
        )
    _apply_payments_to_instalments(invoice)

    config = finance_settings(program)
    updates = {
        "status": InvoiceStatus.ISSUED.value,
        "invoice_number": invoice.get("invoice_number") or _next_invoice_number(config),
        "issued_at": _now(),
        "updated_at": _now(),
        "issued_by": actor_id,
        "instalments": invoice.get("instalments") or [],
        "line_items": invoice.get("line_items") or [],
        "subtotal": money(invoice.get("subtotal")),
        "left_off": invoice.get("left_off") or [],
        "discounts": invoice.get("discounts") or [],
        "discount_total": money(invoice.get("discount_total")),
        "extra_total": money(invoice.get("extra_total")),
        "total_amount": money(invoice.get("total_amount")),
        "concession_reason": invoice.get("concession_reason"),
    }
    updates["status"] = _settle_status({**invoice, **updates})
    firestore_fee_invoices.add_document(str(invoice["id"]), updates)
    logger.info("Issued invoice %s for student %s.",
                updates["invoice_number"], invoice.get("student_id"))
    return {**invoice, **updates}


def cancel_invoice(invoice: dict, reason: str | None = None,
                   actor_id: int | None = None) -> dict:
    """
    Cancels an invoice, keeping it and its payments visible.

    Refused once money has been taken against it. A cancelled bill with payments on it leaves
    the school holding funds against nothing, and the right move there is a refund or a
    credit note, both of which are deliberate acts rather than a side effect of pressing
    cancel.
    """
    if money(invoice.get("amount_paid")) > 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{invoice.get('currency')} {money(invoice.get('amount_paid'))} has been paid "
                "against this invoice. Refund or adjust the payments before cancelling it."
            ),
        )

    updates = {
        "status": InvoiceStatus.CANCELLED.value,
        "cancelled_reason": reason,
        "cancelled_by": actor_id,
        "updated_at": _now(),
    }
    firestore_fee_invoices.add_document(str(invoice["id"]), updates)
    return {**invoice, **updates}


# ---------------------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------------------

def _recompute_paid(invoice: dict) -> float:
    """
    Totals the payments list.

    Recomputed rather than incremented so a duplicated request cannot drift the total away
    from the receipts behind it. An ADJUSTMENT counts like any other payment: a write-off is
    not money, but it does settle the debt, and leaving it out would keep an adjusted invoice
    permanently in arrears.
    """
    return money(sum(money(p.get("amount")) for p in invoice.get("payments") or []))


def _apply_payments_to_instalments(invoice: dict) -> None:
    """
    Spreads what has been paid across the instalment schedule, oldest first.

    Payments naming an instalment are applied to it directly; the rest cascade from the
    earliest unpaid line. Oldest-first is what a school means by "paying off the fees", and it
    is what keeps the late-fee calculation honest - a parent paying one instalment should
    clear the overdue one, not the one that is not due yet.
    """
    instalments = invoice.get("instalments") or []
    if not instalments:
        return

    for line in instalments:
        if line.get("status") != InstalmentStatus.WAIVED.value:
            line["amount_paid"] = 0.0

    # A payment's money, as (label or None, amount) pieces: the fees the office ticked, the
    # one instalment it named, or None for "oldest first".
    pieces = []
    for payment in invoice.get("payments") or []:
        if payment.get("allocations"):
            pieces.extend((a.get("label"), money(a.get("amount"))) for a in payment["allocations"])
        else:
            pieces.append((payment.get("instalment_label"), money(payment.get("amount"))))

    by_label = {line.get("label"): line for line in instalments}
    pool = 0.0
    for label, amount in pieces:
        line = by_label.get(label) if label else None
        if line is not None:
            line["amount_paid"] = money(line.get("amount_paid", 0) + amount)
        else:
            # No label, or one that no longer matches a line because the plan changed after
            # the payment was taken. Spread oldest-first rather than dropped: the money is
            # real whatever the schedule now says.
            pool = money(pool + amount)

    for line in instalments:
        if pool <= 0:
            break
        if line.get("status") == InstalmentStatus.WAIVED.value:
            continue
        owing = money(line.get("amount", 0) - line.get("amount_paid", 0))
        if owing <= 0:
            continue
        applied = min(owing, pool)
        line["amount_paid"] = money(line.get("amount_paid", 0) + applied)
        pool = money(pool - applied)

    for line in instalments:
        if line.get("status") == InstalmentStatus.WAIVED.value:
            continue
        paid = money(line.get("amount_paid", 0))
        total = money(line.get("amount", 0))
        if paid >= total:
            # Includes a line a concession took to zero: nothing is asked of the family.
            line["status"] = InstalmentStatus.PAID.value
        elif paid > 0:
            line["status"] = InstalmentStatus.PARTIALLY_PAID.value
        else:
            line["status"] = InstalmentStatus.PENDING.value


def _settle_status(invoice: dict) -> str:
    """The invoice status implied by what has been paid."""
    if invoice.get("status") == InvoiceStatus.CANCELLED.value:
        return InvoiceStatus.CANCELLED.value

    paid = money(invoice.get("amount_paid"))
    total = money(invoice.get("total_amount"))

    if total > 0 and paid >= total:
        return InvoiceStatus.PAID.value
    if paid > 0:
        return InvoiceStatus.PARTIALLY_PAID.value
    return invoice.get("status") or InvoiceStatus.DRAFT.value


# Razorpay's published standard fees (razorpay.com/pricing, 2026-10): 2% on payments made
# inside India, up to 3% on international cards, with 18% GST on the fee. Passed on to the
# payer for online payments. A currency's own convenience charge, set under
# Fees -> Currencies, takes precedence when the school has negotiated another rate.
RAZORPAY_FEE_PERCENT = {"INR": 2.0}
RAZORPAY_INTERNATIONAL_FEE_PERCENT = 3.0
RAZORPAY_FEE_GST_PERCENT = 18.0


def _gateway_fee_percent(code: str, entry: dict) -> tuple[float, str]:
    own = float(entry.get("convenience_percent") or 0)
    if own > 0:
        return own, "Online payment fee"
    base = RAZORPAY_FEE_PERCENT.get(code, RAZORPAY_INTERNATIONAL_FEE_PERCENT)
    return round(base * (1 + RAZORPAY_FEE_GST_PERCENT / 100.0), 4), \
        f"Razorpay fee ({base:g}% + GST)"


def payment_quote(amount: float, currency: str | None = None, program: str = Program.LMS.value,
                  bill_currency: str | None = None, online: bool = False) -> dict:
    """
    What a payer hands over to settle `amount` of a bill, in the currency they chose.

    The bill stays in its own currency (rupees) and is credited `amount`; this only works out
    what is collected: the amount at the rate set under Fees -> Currencies (1 for the bill's
    own currency), plus that currency's tax and convenience charge when the school set them
    (INR 18% tax, AED 5% VAT). Both are added on top and kept apart from the fees, so the
    bill is credited the fee alone.

    `online` adds the payment gateway's fee too (Razorpay: 2% in India, 3% international,
    plus GST on the fee), charged on the fees and tax together. Cash, cheque and bank
    transfers carry no gateway fee.
    """
    config = currency_settings(program)
    base = config["base_currency"]
    bill = (bill_currency or base).upper()
    code = (currency or bill).upper()
    amount = money(amount)
    currencies = config["currencies"]

    def entry_for(c):
        entry = currencies.get(c)
        if not entry or not entry.get("enabled") or (
                not entry.get("is_base") and float(entry.get("rate_from_base") or 0) <= 0):
            raise HTTPException(
                status_code=400, detail=f"Payments in {c} are not available right now.",
            )
        return entry

    plain = {
        "base_currency": bill, "base_amount": amount, "currency": bill,
        "symbol": (currencies.get(bill) or {}).get("symbol"), "exchange_rate": 1.0,
        "fee_amount": amount, "tax_label": None, "tax_percent": 0.0, "tax_amount": 0.0,
        "convenience_amount": 0.0, "gateway_fee_label": None, "gateway_fee_percent": 0.0,
        "gateway_fee": 0.0, "total": amount, "rate_status": "base", "rate_fetched_at": None,
    }
    entry = entry_for(code)
    bill_entry = entry_for(bill)
    rate = 1.0 if code == bill else (
        float(entry.get("rate_from_base") or 1.0) / float(bill_entry.get("rate_from_base") or 1.0)
    )
    fee = money(amount * rate)
    tax_percent = float(entry.get("tax_percent") or 0) if entry.get("tax_enabled") else 0.0
    tax = money(fee * tax_percent / 100.0) if tax_percent and not entry.get("tax_inclusive") else 0.0
    convenience = 0.0
    gateway_percent, gateway_label, gateway_fee = 0.0, None, 0.0
    if online:
        gateway_percent, gateway_label = _gateway_fee_percent(code, entry)
        gateway_fee = money((fee + tax) * gateway_percent / 100.0
                            + float(entry.get("convenience_amount") or 0))
    return {
        **plain,
        "currency": code,
        "symbol": entry.get("symbol"),
        "exchange_rate": round(rate, 8),
        "fee_amount": fee,
        "tax_label": entry.get("tax_label") or "Tax",
        "tax_percent": tax_percent,
        "tax_amount": tax,
        "convenience_amount": convenience,
        "gateway_fee_label": gateway_label,
        "gateway_fee_percent": gateway_percent,
        "gateway_fee": gateway_fee,
        "total": money(fee + tax + convenience + gateway_fee),
        "rate_status": "base" if code == bill else entry.get("rate_status"),
        "rate_fetched_at": entry.get("rate_fetched_at"),
    }


def _foreign_fields(quote: dict | None) -> dict:
    """What was handed over beside the rupee fees: another currency, tax on top, or both."""
    if not quote or (
        quote.get("currency") == quote.get("base_currency")
        and not quote.get("tax_amount") and not quote.get("convenience_amount")
        and not quote.get("gateway_fee")
    ):
        return {}
    return {
        "paid_currency": quote["currency"],
        "paid_amount": quote["total"],
        "paid_fee_amount": quote["fee_amount"],
        "paid_tax_label": quote.get("tax_label"),
        "paid_tax_percent": quote.get("tax_percent"),
        "paid_tax_amount": quote.get("tax_amount"),
        "paid_convenience_amount": quote.get("convenience_amount"),
        "paid_gateway_fee": quote.get("gateway_fee"),
        "paid_gateway_fee_label": quote.get("gateway_fee_label"),
        "exchange_rate": quote["exchange_rate"],
    }


def clean_allocations(invoice: dict, allocations, amount: float) -> list[dict] | None:
    """
    Checks the fees the office ticked against what each one still owes.

    `allocations` is [{label, amount}]: which fee lines this payment is for, and how much of
    it goes to each, so a parent can pay Books and Term 2 while Term 1 waits. Each must be a
    line on the bill, positive, no more than that line still owes, and together they must
    come to the payment. None when nothing was ticked - the money then settles oldest-first.
    """
    if not allocations:
        return None
    lines = {i.get("label"): i for i in invoice.get("instalments") or []}
    merged: dict[str, float] = {}
    for part in allocations:
        label = _field(part, "label")
        value = money(_field(part, "amount"))
        if value <= 0:
            continue
        line = lines.get(label)
        if line is None or line.get("status") == InstalmentStatus.WAIVED.value:
            raise HTTPException(
                status_code=400, detail=f"There is no fee called '{label}' left to pay on this bill.",
            )
        merged[label] = money(merged.get(label, 0.0) + value)

    currency = invoice.get("currency") or ""
    for label, value in merged.items():
        line = lines[label]
        owing = money(money(line.get("amount")) - money(line.get("amount_paid")))
        if value > owing + 0.009:
            raise HTTPException(
                status_code=400,
                detail=f"{label} has only {currency} {owing} left to pay, not {currency} {value}.",
            )
    total = money(sum(merged.values()))
    if abs(total - money(amount)) > 0.009:
        raise HTTPException(
            status_code=400,
            detail=f"The fees ticked come to {currency} {total}, but the amount is "
                   f"{currency} {money(amount)}.",
        )
    return [{"label": label, "amount": value} for label, value in merged.items()] or None


def issue_left_off(invoice: dict, labels, actor_id: int | None = None, concessions=None,
                   concession_reason: str | None = None, program: str = Program.LMS.value) -> dict:
    """
    Bills fees that were left off when the bill was issued - Term 1 and Term 2 held back
    while only the admission fee was billed, say.

    The lines are priced from the fee rules as they stand now, exactly as issuing would have
    priced them, and join the issued bill (with any concession given now). They then show on
    the family's Fees page like every other line.
    """
    if invoice.get("status") not in FROZEN_STATUSES:
        raise HTTPException(status_code=400, detail="Issue the bill first.")
    wanted = [str(l) for l in labels or [] if l]
    left_off = list(invoice.get("left_off") or [])
    if not wanted:
        raise HTTPException(status_code=400, detail="Tick the fees to add to the bill.")
    for label in wanted:
        if label not in left_off:
            raise HTTPException(status_code=400, detail=f"{label} is not waiting to be billed on this bill.")

    student = require_document(firestore_users, invoice["student_id"], "Student")
    breakdown = finance.compute_breakdown(student, invoice["academic_year_id"], program,
                                          currency=invoice.get("currency"))
    priced = {i.get("label"): i for i in breakdown.get("instalments") or []}

    instalments = [dict(i) for i in invoice.get("instalments") or []]
    line_items = [dict(l) for l in invoice.get("line_items") or []]
    item_by_name = {l.get("name"): l for l in line_items}
    source_items = {l.get("name"): l for l in breakdown.get("line_items") or []}
    added = 0.0
    for label in wanted:
        line = priced.get(label)
        if not line or money(line.get("amount")) <= 0:
            raise HTTPException(
                status_code=400,
                detail=f"{label} no longer has a fee set in the fee structure, so it cannot be billed.",
            )
        line = {**line, "amount_paid": 0.0, "status": InstalmentStatus.PENDING.value,
                "added_by": actor_id, "added_at": _now()}
        instalments.append(line)
        for part in line.get("components") or []:
            share = money(part.get("amount"))
            item = item_by_name.get(part.get("name"))
            if item is None:
                base = source_items.get(part.get("name")) or {"name": part.get("name")}
                item = {**base, "amount": 0.0, "net_amount": 0.0, "discount_amount": 0.0}
                line_items.append(item)
                item_by_name[item.get("name")] = item
            item["amount"] = money(money(item.get("amount")) + share)
            item["net_amount"] = money(money(item.get("net_amount", item.get("amount"))) + share)
        added = money(added + money(line.get("amount")))

    # Keep the schedule in due-date order, extra items last.
    instalments.sort(key=lambda i: (bool(i.get("is_extra")), str(i.get("due_date") or "")))
    updated = {
        **invoice,
        "instalments": instalments,
        "line_items": line_items,
        "subtotal": money(money(invoice.get("subtotal")) + added),
        "total_amount": money(money(invoice.get("total_amount")) + added),
        "left_off": [l for l in left_off if l not in wanted],
    }
    updated = _apply_concessions(updated, concessions, concession_reason, actor_id)
    _apply_payments_to_instalments(updated)
    updated["status"] = _settle_status(updated)
    updated["updated_at"] = _now()
    firestore_fee_invoices.add_document(str(invoice["id"]), {
        key: updated.get(key) for key in (
            "instalments", "line_items", "subtotal", "total_amount", "left_off", "discounts",
            "discount_total", "status", "updated_at",
        )
    })
    logger.info("Billed %s more on invoice %s: %s.", added, invoice["id"], ", ".join(wanted))
    return updated


def add_extra_items(invoice: dict, items, actor_id: int | None = None) -> dict:
    """Adds extra items (books, uniform) to a bill that has already been issued."""
    if invoice.get("status") not in FROZEN_STATUSES:
        raise HTTPException(
            status_code=400,
            detail="Issue the bill first. Extra items on a bill that is not issued yet are "
                   "added when you issue it.",
        )
    updated = _add_extra_lines(invoice, items, actor_id)
    _apply_payments_to_instalments(updated)
    updated["status"] = _settle_status(updated)
    updated["updated_at"] = _now()
    firestore_fee_invoices.add_document(str(invoice["id"]), {
        "instalments": updated["instalments"],
        "extra_total": money(updated.get("extra_total")),
        "total_amount": money(updated.get("total_amount")),
        "status": updated["status"],
        "updated_at": updated["updated_at"],
    })
    return updated


def remove_extra_item(invoice: dict, label: str) -> dict:
    """Takes an extra item off again, while nothing has been paid towards it."""
    instalments = [dict(i) for i in invoice.get("instalments") or []]
    line = next((i for i in instalments if i.get("label") == label), None)
    if line is None or not line.get("is_extra"):
        raise HTTPException(
            status_code=404, detail=f"There is no extra item called '{label}' on this bill.",
        )
    if money(line.get("amount_paid")) > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Money has already been paid towards {label}, so it cannot be removed.",
        )
    amount = money(line.get("amount"))
    updated = dict(invoice)
    updated["instalments"] = [i for i in instalments if i is not line]
    updated["extra_total"] = money(money(invoice.get("extra_total")) - amount)
    updated["total_amount"] = money(money(invoice.get("total_amount")) - amount)
    _apply_payments_to_instalments(updated)
    updated["status"] = _settle_status(updated)
    updated["updated_at"] = _now()
    firestore_fee_invoices.add_document(str(invoice["id"]), {
        "instalments": updated["instalments"],
        "extra_total": updated["extra_total"],
        "total_amount": updated["total_amount"],
        "status": updated["status"],
        "updated_at": updated["updated_at"],
    })
    return updated


def record_payment(invoice: dict, payload, actor_id: int | None = None) -> dict:
    """
    Records money received against an invoice.

    Overpayment is refused rather than accepted and carried as a credit. A school that takes
    more than it billed has almost always billed the wrong amount or typed an extra zero, and
    discovering that at the point of entry is far cheaper than discovering it in a
    reconciliation three months later.
    """
    amount = money(payload.amount)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="A payment must be a positive amount.")

    if invoice.get("status") == InvoiceStatus.CANCELLED.value:
        raise HTTPException(
            status_code=400, detail="This invoice has been cancelled; it cannot take payments."
        )

    outstanding = money(money(invoice.get("total_amount")) - money(invoice.get("amount_paid")))
    if amount > outstanding + 0.009:
        raise HTTPException(
            status_code=400,
            detail=(
                f"That is more than the {invoice.get('currency')} {outstanding} outstanding. "
                "Check the amount, or adjust the invoice if the fees have changed."
            ),
        )

    if payload.instalment_label:
        labels = [i.get("label") for i in invoice.get("instalments") or []]
        if payload.instalment_label not in labels:
            raise HTTPException(
                status_code=400,
                detail=f"No instalment named '{payload.instalment_label}' on this invoice. "
                       f"Expected one of: {', '.join(str(l) for l in labels)}.",
            )

    allocations = clean_allocations(invoice, getattr(payload, "allocations", None), amount)
    # Paid in another currency: the gateway passes the quote it charged; the counter names
    # the currency and it is priced here, at today's rate.
    quote = getattr(payload, "quote", None)
    if quote is None and getattr(payload, "currency", None):
        quote = payment_quote(amount, payload.currency, invoice.get("program") or Program.LMS.value,
                              invoice.get("currency"))

    method = getattr(payload.method, "value", payload.method)
    payment = {
        "amount": amount,
        "paid_at": (payload.paid_at.isoformat() if payload.paid_at else _now()),
        "method": method,
        "reference": payload.reference,
        "instalment_label": None if allocations else payload.instalment_label,
        "allocations": allocations,
        "note": payload.note,
        "recorded_by": actor_id,
        "recorded_at": _now(),
        **_foreign_fields(quote),
    }

    # An adjustment settles a debt without being money, so it gets no receipt number:
    # a gap-free receipt series is what an auditor checks against the cash book.
    is_money = method != PaymentMethod.ADJUSTMENT.value
    year = admission_service.firestore_academic_years.get_document(
        str(invoice.get("academic_year_id"))
    ) or {}

    with _receipt_lock:
        if is_money:
            payment["receipt_number"] = _next_receipt_number(year.get("name"))

        updated = dict(invoice)
        updated["payments"] = list(invoice.get("payments") or []) + [payment]
        updated["amount_paid"] = _recompute_paid(updated)
        _apply_payments_to_instalments(updated)
        updated["status"] = _settle_status(updated)
        updated["updated_at"] = _now()

        firestore_fee_invoices.add_document(str(invoice["id"]), {
            "payments": updated["payments"],
            "amount_paid": updated["amount_paid"],
            "instalments": updated["instalments"],
            "status": updated["status"],
            "updated_at": updated["updated_at"],
        })
    logger.info("Recorded %s %s against invoice %s (now %s).",
                updated["currency"], amount, invoice["id"], updated["status"])
    if is_money:
        _email_receipt_later(updated, len(updated["payments"]) - 1)
    return updated


def receipt_recipients(student: dict) -> list[dict]:
    """
    Who a fee receipt is emailed to: the student, the parent email on their profile, and
    every active parent account linked to them that may see fees. One email per address.
    """
    from app.services import families as family_service

    found: dict[str, dict] = {}

    def add(email, name, view_path):
        key = str(email or "").strip().lower()
        if key and "@" in key and key not in found:
            found[key] = {"email": str(email).strip(), "name": name, "view_path": view_path}

    add(student.get("email"), student.get("full_name"), "/student/fees")
    add(student.get("guardian_email"), student.get("guardian_name"), None)
    for link in family_service.links_for_student(student["id"]):
        if not link.get("may_view_fees"):
            continue
        parent = firestore_users.get_document(str(link["parent_id"])) or {}
        if parent.get("is_active", True):
            add(parent.get("email"), parent.get("full_name"), "/parent")
    return list(found.values())


def email_receipt(invoice: dict, index: int) -> list[str]:
    """Emails the receipt for `invoice.payments[index]`; returns the addresses it went to."""
    from app.core import mailer
    from app.services import finance_reports

    if not mailer.is_configured():
        logger.info("Mail is not set up; receipt for invoice %s not emailed.", invoice.get("id"))
        return []
    student = firestore_users.get_document(str(invoice.get("student_id"))) or {}
    if not student:
        return []
    student = {**student, "id": invoice.get("student_id")}
    receipt = finance_reports.payment_receipt(invoice, index)
    method = {
        PaymentMethod.ONLINE.value: "Online",
        PaymentMethod.BANK_TRANSFER.value: "Bank transfer",
    }.get(str(receipt.get("method") or ""), str(receipt.get("method") or "").replace("_", " ").title())
    sent = []
    for person in receipt_recipients(student):
        url = mailer.absolute_link(person["view_path"] or "/")
        if mailer.send_fee_receipt_email(person["email"], person["name"], receipt, url, method):
            sent.append(person["email"])
    logger.info("Receipt %s emailed to %d address(es).", receipt.get("receipt_number"), len(sent))

    # And the office: one alert per address in School Settings -> Payment alert emails.
    from app.services.tuition.settings_store import school_profile

    counter = mailer.absolute_link(f"/admin/fee-collection?student={invoice.get('student_id')}")
    office = {e.strip().lower() for e in (school_profile().get("fee_alert_emails") or "").replace(";", ",").split(",")}
    for address in sorted(e for e in office if "@" in e):
        mailer.send_fee_payment_alert_email(address, receipt, counter, method)
    return sent


def _email_receipt_later(invoice: dict, index: int) -> None:
    """Emails the receipt off the request, so a slow or broken mail service never holds up a payment."""
    def run():
        try:
            email_receipt(invoice, index)
        except Exception:  # noqa: BLE001 - a receipt email must never break the payment
            logger.exception("Emailing the receipt for invoice %s failed.", invoice.get("id"))

    threading.Thread(target=run, name="receipt-email", daemon=True).start()


def waive_instalment(invoice: dict, label: str, reason: str,
                     actor_id: int | None = None) -> dict:
    """
    Writes off one instalment.

    The invoice total is reduced by the waived amount rather than the instalment merely being
    marked settled, because the parent is genuinely not being asked for it and an arrears
    report that still counts it is wrong. The reason is mandatory - a waiver with no stated
    cause is indistinguishable from a mistake when somebody audits it next year.
    """
    if not str(reason or "").strip():
        raise HTTPException(
            status_code=400, detail="A waiver needs a reason recorded against it."
        )

    instalments = [dict(i) for i in invoice.get("instalments") or []]
    for line in instalments:
        if line.get("label") != label:
            continue
        if line.get("status") == InstalmentStatus.WAIVED.value:
            raise HTTPException(status_code=400, detail="That instalment is already waived.")

        waived = money(line.get("amount", 0) - line.get("amount_paid", 0))
        line["status"] = InstalmentStatus.WAIVED.value
        line["waived_reason"] = reason
        line["waived_by"] = actor_id
        line["waived_amount"] = waived

        updated = dict(invoice)
        updated["instalments"] = instalments
        updated["total_amount"] = money(money(invoice.get("total_amount")) - waived)
        updated["status"] = _settle_status(updated)
        updated["updated_at"] = _now()

        firestore_fee_invoices.add_document(str(invoice["id"]), {
            "instalments": instalments,
            "total_amount": updated["total_amount"],
            "status": updated["status"],
            "updated_at": updated["updated_at"],
        })
        return updated

    raise HTTPException(status_code=404, detail=f"No instalment named '{label}' on this invoice.")


def apply_late_fees(invoice: dict, program: str = Program.LMS.value,
                    on: date | None = None) -> dict:
    """
    Charges the configured late fee on every overdue instalment.

    Idempotent per instalment: a line already carrying a late fee is skipped, so running the
    sweep twice in a day does not charge twice. Late fees are never taxed - a penalty is not
    a service - which is why this adds to `late_fee_total` rather than going through the
    breakdown.
    """
    config = finance_settings(program)
    if not config["late_fee_enabled"]:
        return invoice

    today = on or date.today()
    grace = int(config["late_fee_grace_days"])

    instalments = [dict(i) for i in invoice.get("instalments") or []]
    charged = 0.0

    for line in instalments:
        if line.get("late_fee_amount"):
            continue
        view = finance.instalment_view(line, grace, today)
        if not view.get("is_overdue"):
            continue

        outstanding = money(line.get("amount", 0) - line.get("amount_paid", 0))
        fee = money(
            outstanding * float(config["late_fee_percent"]) / 100.0
            + float(config["late_fee_amount"])
        )
        if fee <= 0:
            continue

        line["late_fee_amount"] = fee
        line["late_fee_charged_at"] = _now()
        charged = money(charged + fee)

    if charged <= 0:
        return invoice

    updates = {
        "instalments": instalments,
        "late_fee_total": money(money(invoice.get("late_fee_total")) + charged),
        "total_amount": money(money(invoice.get("total_amount")) + charged),
        "updated_at": _now(),
    }
    firestore_fee_invoices.add_document(str(invoice["id"]), updates)
    logger.info("Charged %s in late fees on invoice %s.", charged, invoice["id"])
    return {**invoice, **updates}


def collect_payment(student_id, payload, actor_id: int | None = None,
                    program: str = Program.LMS.value) -> dict:
    """
    The counter: a student pays, and the office records it in one step.

    Finds the year's invoice and records the payment on it. The bill has to have been issued
    first: issuing is the office's deliberate act, and it is what puts the bill on the
    student's and parent's Fees page, so the counter never raises one as a side effect of
    taking money. Refused, with the reason, when there is no issued bill or the amount
    exceeds what is outstanding.
    """
    require_document(firestore_users, student_id, "Student")
    academic_year_id = getattr(payload, "academic_year_id", None)
    if academic_year_id is None:
        year = admission_service.current_year(program)
        if not year:
            raise HTTPException(
                status_code=400,
                detail="No academic year is configured. Create one before collecting fees.",
            )
        academic_year_id = year["id"]

    invoice = firestore_fee_invoices.get_document(invoice_id_for(student_id, academic_year_id))
    if invoice is None or invoice.get("status") in (
        InvoiceStatus.DRAFT.value, InvoiceStatus.CANCELLED.value,
    ):
        raise HTTPException(
            status_code=400,
            detail="This student's fee bill for the year has not been issued yet. "
                   "Issue the bill first, then collect the payment.",
        )

    return record_payment(invoice, payload, actor_id)


# ---------------------------------------------------------------------------------------
# Presenting
# ---------------------------------------------------------------------------------------

def present_invoice(invoice: dict, program: str = Program.LMS.value,
                    on: date | None = None) -> dict:
    """
    An invoice as the API returns it, with the clock-derived fields resolved.

    Instalment status and the outstanding balance are computed here rather than read from
    storage; see `finance.instalment_view` for why OVERDUE is never stored.
    """
    config = finance_settings(program)
    grace = int(config["late_fee_grace_days"])

    student = firestore_users.get_document(str(invoice.get("student_id"))) or {}
    year = admission_service.firestore_academic_years.get_document(
        str(invoice.get("academic_year_id"))
    ) or {}

    instalments = [
        finance.instalment_view(i, grace, on) for i in invoice.get("instalments") or []
    ]
    total = money(invoice.get("total_amount"))
    paid = money(invoice.get("amount_paid"))

    currencies = currency_settings(program)

    return {
        "id": invoice["id"],
        "invoice_number": invoice.get("invoice_number"),
        "student_id": int(invoice["student_id"]),
        "student_name": student.get("full_name"),
        "admission_number": student.get("admission_number"),
        "academic_year_id": int(invoice["academic_year_id"]),
        "academic_year_name": year.get("name"),
        "structure_id": invoice.get("structure_id"),
        "instalment_plan_id": invoice.get("instalment_plan_id"),
        "status": invoice.get("status") or InvoiceStatus.DRAFT.value,
        # Read back from the invoice, not recomputed. An issued bill must report the rate it
        # was issued at even after the school has changed it.
        "currency": invoice.get("currency") or config["currency"],
        "currency_symbol": invoice.get("currency_symbol"),
        "base_currency": invoice.get("base_currency") or currencies["base_currency"],
        "exchange_rate": float(invoice.get("exchange_rate") or 1.0),
        "available_currencies": currencies["available"],

        "line_items": invoice.get("line_items") or [],
        "discounts": invoice.get("discounts") or [],
        "instalments": instalments,
        # Fee lines held back when the bill was issued; they can be billed later.
        "left_off": invoice.get("left_off") or [],
        "payments": invoice.get("payments") or [],

        "subtotal": money(invoice.get("subtotal")),
        "discount_total": money(invoice.get("discount_total")),
        "taxable_base": money(invoice.get("taxable_base")),
        "tax_total": money(invoice.get("tax_total")),
        "tax_label": config["tax_label"],
        "charge_total": money(invoice.get("charge_total")),
        "convenience_total": money(invoice.get("convenience_total")),
        "late_fee_total": money(invoice.get("late_fee_total")),
        "total_amount": total,
        "amount_paid": paid,
        "amount_outstanding": money(total - paid),
        "is_overdue": any(i.get("is_overdue") for i in instalments),

        "issued_at": invoice.get("issued_at"),
        "due_date": invoice.get("due_date"),
        "notes": invoice.get("notes"),
        "gateway_enabled": gateway_enabled(program),
        "gateway_provider": gateway_provider(program),
        "created_at": invoice.get("created_at"),
        "updated_at": invoice.get("updated_at"),
    }


def invoices_for_student(student_id) -> list[dict]:
    invoices = firestore_fee_invoices.query_documents("student_id", "==", int(student_id))
    return sorted(invoices, key=lambda i: str(i.get("academic_year_id") or ""), reverse=True)


def issued_invoices_for_student(student_id) -> list[dict]:
    """What a student or parent sees: bills the office has issued, never its drafts."""
    return [
        i for i in invoices_for_student(student_id)
        if i.get("status") not in (None, InvoiceStatus.DRAFT.value, InvoiceStatus.CANCELLED.value)
    ]


# ---------------------------------------------------------------------------------------
# The gateway seam
# ---------------------------------------------------------------------------------------

def require_intent(intent_id) -> dict:
    return require_document(firestore_payment_intents, intent_id, "Payment intent")


def assert_payable(invoice: dict) -> None:
    """
    Refuses to start a payment on a bill nobody is being asked to pay.

    A draft is the office's working copy and can still change; a cancelled invoice is no
    longer owed; a settled one has nothing left. Each is a 400 with the reason, because
    "payment failed" on a bill that was never payable is a support call.
    """
    status_value = invoice.get("status")
    if status_value == InvoiceStatus.DRAFT.value:
        raise HTTPException(
            status_code=400,
            detail="This invoice has not been issued yet, so it is not payable.",
        )
    if status_value == InvoiceStatus.CANCELLED.value:
        raise HTTPException(status_code=400, detail="This invoice has been cancelled.")
    outstanding = money(money(invoice.get("total_amount")) - money(invoice.get("amount_paid")))
    if outstanding <= 0:
        raise HTTPException(status_code=400, detail="This invoice is already settled.")


def intent_reference(intent_id) -> str:
    """What the payer quotes at the office or on a transfer. Short, and never a secret."""
    return f"PAY-{intent_id}"


def gateway_enabled(program: str = Program.LMS.value) -> bool:
    """Online payment is on when the school switched it on, or when Razorpay keys are set."""
    return bool(finance_settings(program)["gateway_enabled"]) or razorpay.is_configured()


def gateway_provider(program: str = Program.LMS.value) -> str | None:
    return "razorpay" if razorpay.is_configured() else finance_settings(program)["gateway_provider"]


def create_intent(invoice: dict, amount: float, actor_id: int | None = None,
                  instalment_label: str | None = None,
                  program: str = Program.LMS.value,
                  requested_method: str | None = None,
                  allocations=None, currency: str | None = None) -> dict:
    """
    Records an intention to pay, and hands back what the checkout page needs next.

    `amount` is what the bill is credited, in the bill's currency. `currency` is what the
    payer pays in; the quote for it (rate, tax) is frozen on the intent, so the money
    charged and the money credited are fixed together and cannot drift apart.

    For an online method no provider is wired, so `checkout_url` comes back null and the
    intent sits in CREATED. That is deliberate rather than a stub: the payment page already
    shows the amount and the breakdown, and when an adapter is written it fills in
    `provider`, `provider_reference` and `checkout_url` without this shape changing.

    For an offline method - a bank transfer or a payment at the office - the intent IS the
    product: a reference the payer quotes and the office matches when the money arrives.
    Nothing is credited here; `record_payment` does that when the office confirms it.

    The idempotency key is generated here rather than by the provider because it has to exist
    before the first call to one - it is what makes a retried webhook safe, and a key minted
    by the thing being retried guarantees nothing.
    """
    config = finance_settings(program)
    amount = money(amount)

    if amount <= 0:
        raise HTTPException(status_code=400, detail="A payment must be a positive amount.")
    assert_payable(invoice)

    outstanding = money(money(invoice.get("total_amount")) - money(invoice.get("amount_paid")))
    if amount > outstanding + 0.009:
        raise HTTPException(
            status_code=400,
            detail=f"That is more than the {invoice.get('currency')} {outstanding} outstanding.",
        )

    allocations = clean_allocations(invoice, allocations, amount)
    method = str(requested_method).upper() if requested_method else None
    offline = method in OFFLINE_GATEWAY_METHODS
    use_razorpay = not offline and razorpay.is_configured()
    charge = payment_quote(amount, currency, program, invoice.get("currency"), online=use_razorpay)

    intent_id = firestore_payment_intents.get_next_numeric_id()
    document = {
        "invoice_id": invoice["id"],
        "invoice_number": invoice.get("invoice_number") or str(invoice["id"]),
        "student_id": int(invoice["student_id"]),
        "program": str(program).upper(),
        "amount": amount,
        "currency": invoice.get("currency") or config["currency"],
        "status": PaymentIntentStatus.CREATED.value,
        "provider": None if offline else gateway_provider(program),
        "provider_reference": None,
        "checkout_url": None,
        "idempotency_key": uuid.uuid4().hex,
        "instalment_label": None if allocations else instalment_label,
        "allocations": allocations,
        "charge": charge,
        "requested_method": method,
        "provider_payload": {},
        "initiated_by": actor_id,
        "created_at": _now(),
    }
    firestore_payment_intents.add_document(str(intent_id), document)
    document["id"] = intent_id
    document["reference"] = intent_reference(intent_id)

    if use_razorpay:
        document["checkout"] = _razorpay_checkout(invoice, document, program)
        document["provider_reference"] = document["checkout"]["order_id"]
        firestore_payment_intents.add_document(str(intent_id), {
            "provider_reference": document["provider_reference"],
        })
        document["detail"] = (
            "Razorpay test mode: no real money moves." if document["checkout"]["test_mode"]
            else None
        )
    elif offline:
        document["detail"] = (
            f"Quote {document['reference']} when you pay. The office records the payment "
            "against this invoice and it updates here as soon as they do."
        )
    elif not config["gateway_enabled"]:
        document["detail"] = (
            "Online payment is not switched on. Your request is saved; pay at the office "
            f"or by bank transfer quoting {document['reference']}."
        )
    else:
        document["detail"] = (
            f"{config['gateway_provider'] or 'The payment provider'} is selected but not "
            f"connected yet. Your request is saved; pay at the office or by bank transfer "
            f"quoting {document['reference']}."
        )
    return document


def settle_intent(intent: dict, status: str, provider_reference: str | None = None,
                  payload: dict | None = None,
                  failure_reason: str | None = None) -> dict:
    """
    Moves an intent to a provider-reported state, crediting the invoice on success.

    The idempotency guarantee lives here: an intent already in a terminal state is returned
    unchanged rather than re-applied, so a webhook delivered three times credits one payment.
    This is the part of a gateway integration that cannot be added afterwards - by the time
    duplicates are noticed, the double-credited invoices are already reconciled.
    """
    if intent.get("status") in TERMINAL_INTENT_STATES:
        logger.info("Intent %s is already %s; ignoring a repeat callback.",
                    intent["id"], intent["status"])
        return intent

    updates = {
        "status": status,
        "provider_reference": provider_reference or intent.get("provider_reference"),
        "provider_payload": payload or intent.get("provider_payload") or {},
        "failure_reason": failure_reason,
        "updated_at": _now(),
    }
    if status in TERMINAL_INTENT_STATES:
        updates["completed_at"] = _now()

    firestore_payment_intents.add_document(str(intent["id"]), updates)
    settled = {**intent, **updates}
    settled["reference"] = intent_reference(intent["id"])

    if status == PaymentIntentStatus.SUCCEEDED.value:
        reference = provider_reference or intent.get("idempotency_key")
        # Two billers, one seam. The intent carries which one it belongs to, so a tuition
        # payment credits the tuition invoice through the tuition module's own recorder
        # rather than being forced through the school's instalment logic.
        if str(intent.get("program") or "").upper() == Program.TUITION.value:
            from app.services.tuition import fees as tuition_fees

            invoice = tuition_fees.require_invoice(intent["invoice_id"])
            tuition_fees.record_payment(
                invoice, {"id": intent.get("initiated_by")}, intent["amount"],
                method=PaymentMethod.ONLINE.value, reference=reference,
            )
        else:
            invoice = require_invoice(intent["invoice_id"])

            def gateway_payment(allocations):
                return type("GatewayPayment", (), {
                    "amount": intent["amount"],
                    "paid_at": None,
                    "method": PaymentMethod.ONLINE,
                    "reference": reference,
                    "instalment_label": intent.get("instalment_label"),
                    "allocations": allocations,
                    "quote": intent.get("charge"),
                    "note": f"Paid online via {(intent.get('provider') or 'the gateway').title()}",
                })()

            try:
                record_payment(invoice, gateway_payment(intent.get("allocations")),
                               actor_id=intent.get("initiated_by"))
            except HTTPException:
                if not intent.get("allocations"):
                    raise
                # The fees chosen were paid some other way while the payer was on the
                # gateway. The money is real, so it settles oldest-first instead.
                logger.warning("Intent %s: chosen fees no longer open; settling oldest-first.",
                               intent["id"])
                record_payment(invoice, gateway_payment(None), actor_id=intent.get("initiated_by"))
        logger.info("Intent %s succeeded; credited invoice %s.",
                    intent["id"], intent["invoice_id"])

    return settled


def _razorpay_checkout(invoice: dict, intent: dict, program: str) -> dict:
    """
    Opens a Razorpay order for the intent and returns what the browser's Checkout needs.

    Orders rather than Payment Links: the payer is already on our page, and Checkout opens
    over it and comes back with a signature this server can check.
    """
    student = firestore_users.get_document(str(invoice.get("student_id"))) or {}
    try:
        charge = intent.get("charge") or {}
        order = razorpay.create_order(
            amount=charge.get("total", intent["amount"]),
            currency=charge.get("currency", intent["currency"]),
            receipt=intent_reference(intent["id"]),
            notes={
                "invoice": intent.get("invoice_number"),
                "student": student.get("full_name"),
                "admission_number": student.get("admission_number"),
                "program": program,
            },
        )
    except razorpay.RazorpayError as exc:
        firestore_payment_intents.add_document(str(intent["id"]), {
            "status": PaymentIntentStatus.FAILED.value,
            "failure_reason": str(exc),
            "completed_at": _now(),
        })
        charged_in = (intent.get("charge") or {}).get("currency") or intent["currency"]
        if charged_in != "INR":
            # Razorpay takes other currencies only once it has switched on international
            # payments for the account; until then every AED order is refused.
            raise HTTPException(
                status_code=400,
                detail=f"Online payment in {charged_in} is not available yet. Pay in INR, or pay "
                       f"in {charged_in} at the school office.",
            ) from exc
        raise HTTPException(
            status_code=502,
            detail="Online payment could not be started just now. Please try again in a "
                   "minute, or pay at the school office.",
        ) from exc

    return {
        "provider": "razorpay",
        "key_id": razorpay.key_id(),
        "order_id": order["id"],
        "amount_minor": int(order.get("amount") or 0),
        "currency": order.get("currency") or intent["currency"],
        "description": f"Fees {intent.get('invoice_number') or ''}".strip(),
        "prefill": {
            "name": student.get("guardian_name") or student.get("full_name"),
            "email": student.get("guardian_email") or student.get("email"),
            "contact": student.get("guardian_phone") or student.get("phone"),
        },
        "test_mode": razorpay.is_test_mode(),
    }


def confirm_razorpay(intent: dict, order_id: str, payment_id: str, signature: str) -> dict:
    """
    Credits a Razorpay payment once its signature checks out.

    The browser hands back three values after Checkout; only the signature, made with the
    school's secret key, proves Razorpay sent them. The payment is then read back from
    Razorpay and captured if the account does not capture automatically, so the receipt is
    only printed for money the school will actually receive. Repeat calls are harmless:
    `settle_intent` ignores an intent that is already settled.
    """
    if (intent.get("provider") or "").lower() != "razorpay" or \
            str(intent.get("provider_reference") or "") != str(order_id or ""):
        raise HTTPException(status_code=400, detail="This payment does not belong to this request.")
    if not razorpay.verify_payment_signature(order_id, payment_id, signature):
        raise HTTPException(
            status_code=400,
            detail="The payment could not be confirmed. If money left your account, contact "
                   "the school office with the payment ID.",
        )
    if intent.get("status") in TERMINAL_INTENT_STATES:
        return {**intent, "reference": intent_reference(intent["id"])}

    try:
        payment = razorpay.fetch_payment(payment_id)
        if payment.get("status") == "authorized":
            charge = intent.get("charge") or {}
            payment = razorpay.capture_payment(
                payment_id, charge.get("total", intent["amount"]),
                charge.get("currency", intent["currency"]),
            )
    except razorpay.RazorpayError as exc:
        logger.error("Razorpay payment %s for intent %s could not be read: %s",
                     payment_id, intent["id"], exc)
        raise HTTPException(
            status_code=502,
            detail="Your payment went through but could not be confirmed yet. Refresh the page "
                   "in a minute; if it still shows unpaid, contact the school office.",
        ) from exc

    if payment.get("status") != "captured":
        return settle_intent(
            intent, PaymentIntentStatus.FAILED.value, payment_id,
            {"order_id": order_id, "payment_id": payment_id, "status": payment.get("status")},
            failure_reason=payment.get("error_description") or f"Payment {payment.get('status')}",
        )
    return settle_intent(
        intent, PaymentIntentStatus.SUCCEEDED.value, payment_id,
        {"order_id": order_id, "payment_id": payment_id, "method": payment.get("method")},
    )


def payer_breakdown(student: dict, academic_year_id=None, program: str = Program.LMS.value,
                    currency: str | None = None) -> dict:
    """
    What a student or parent is shown: the bill the office issued, never a live estimate.

    Before the office issues the year's bill there is nothing to show, so this is a 404 with
    a plain sentence. Afterwards the figures come from the issued bill itself, so the
    concessions and extra items the office added appear exactly as issued. Another currency
    can still be picked for display; the figures are converted at today's rate.
    """
    if academic_year_id is None:
        year = admission_service.current_year(program)
        academic_year_id = year["id"] if year else None
    invoice = (
        firestore_fee_invoices.get_document(invoice_id_for(student["id"], academic_year_id))
        if academic_year_id is not None else None
    )
    if not invoice or invoice.get("status") in (
        None, InvoiceStatus.DRAFT.value, InvoiceStatus.CANCELLED.value,
    ):
        raise HTTPException(status_code=404, detail="The school has not issued a fee bill for this year yet.")

    view = present_invoice({**invoice, "id": invoice.get("id") or invoice_id_for(student["id"], academic_year_id)}, program)
    options, recommended, rate = [], None, 1.0
    try:
        live = finance.compute_breakdown(student, academic_year_id, program, currency=currency)
        options = live.get("currency_options") or []
        recommended = live.get("recommended_currency")
        if currency and currency.upper() != (view["currency"] or "").upper():
            rate = float(live.get("exchange_rate") or 1.0) / float(view["exchange_rate"] or 1.0)
            view["currency"] = live["currency"]
            view["currency_symbol"] = live.get("currency_symbol")
            view["exchange_rate"] = live.get("exchange_rate")
    except HTTPException:
        pass

    def conv(value):
        return money(money(value) * rate)

    lines = [
        {**l, **{k: conv(l.get(k)) for k in ("amount", "net_amount", "discount_amount", "tax_amount")
                  if l.get(k) is not None}}
        for l in view["line_items"]
    ]
    instalments = [
        {**i,
         "amount": conv(i.get("amount")),
         "amount_paid": conv(i.get("amount_paid")),
         "outstanding": conv(i.get("outstanding")) if i.get("outstanding") is not None else None,
         "components": [{**c, "amount": conv(c.get("amount"))} for c in i.get("components") or []]}
        for i in view["instalments"]
    ]
    return {
        "student_id": view["student_id"],
        "student_name": view["student_name"],
        "academic_year_id": view["academic_year_id"],
        "currency": view["currency"],
        "currency_symbol": view.get("currency_symbol"),
        "base_currency": view.get("base_currency"),
        "exchange_rate": float(view.get("exchange_rate") or 1.0),
        "available_currencies": view["available_currencies"],
        "recommended_currency": recommended,
        "currency_options": options,
        "structure_id": view.get("structure_id"),
        "structure_name": f"Fee bill {view.get('invoice_number')}" if view.get("invoice_number") else None,
        "instalment_plan_id": view.get("instalment_plan_id"),
        "line_items": lines,
        "discounts": [{**d, "amount": conv(d.get("amount"))} for d in view["discounts"]],
        "instalments": instalments,
        "subtotal": conv(view["subtotal"]),
        "discount_total": conv(view["discount_total"]),
        "taxable_base": conv(view["taxable_base"]),
        "tax_total": conv(view["tax_total"]),
        "tax_label": view["tax_label"],
        "charge_total": conv(view["charge_total"]),
        "convenience_total": conv(view["convenience_total"]),
        "total_amount": conv(view["total_amount"]),
        "gateway_enabled": view["gateway_enabled"],
        "gateway_provider": view["gateway_provider"],
    }


def intents_for_invoice(invoice_id) -> list[dict]:
    intents = firestore_payment_intents.query_documents("invoice_id", "==", invoice_id)
    for intent in intents:
        intent.setdefault("reference", intent_reference(intent.get("id")))
    return sorted(intents, key=lambda i: str(i.get("created_at") or ""), reverse=True)
