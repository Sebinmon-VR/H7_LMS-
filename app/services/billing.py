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
import uuid
from datetime import date, datetime, timedelta

from fastapi import HTTPException

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


def issue_invoice(invoice: dict, actor_id: int | None = None,
                  program: str = Program.LMS.value) -> dict:
    """
    Freezes an invoice and gives it a number.

    A zero-total invoice is refused: it means no fee structure matched, and sending a parent
    a bill for nothing is a support call rather than a courtesy.
    """
    if invoice.get("status") in FROZEN_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"This invoice is already {invoice.get('status')}.",
        )
    if money(invoice.get("total_amount")) <= 0:
        raise HTTPException(
            status_code=400,
            detail="This invoice totals zero - no fee structure applies to the student. "
                   "Set up a structure for their class or category first.",
        )

    config = finance_settings(program)
    updates = {
        "status": InvoiceStatus.ISSUED.value,
        "invoice_number": invoice.get("invoice_number") or _next_invoice_number(config),
        "issued_at": _now(),
        "updated_at": _now(),
        "issued_by": actor_id,
    }
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

    targeted, general = [], []
    for payment in invoice.get("payments") or []:
        (targeted if payment.get("instalment_label") else general).append(payment)

    for payment in targeted:
        for line in instalments:
            if line.get("label") == payment["instalment_label"]:
                line["amount_paid"] = money(line.get("amount_paid", 0) + money(payment["amount"]))
                break
        else:
            # The label no longer matches any line - the plan was changed after the payment
            # was taken. Treated as a general payment rather than dropped, because the money
            # is real whatever the schedule now says.
            general.append(payment)

    pool = money(sum(money(p["amount"]) for p in general))
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
        if paid >= total and total > 0:
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

    payment = {
        "amount": amount,
        "paid_at": (payload.paid_at.isoformat() if payload.paid_at else _now()),
        "method": getattr(payload.method, "value", payload.method),
        "reference": payload.reference,
        "instalment_label": payload.instalment_label,
        "note": payload.note,
        "recorded_by": actor_id,
        "recorded_at": _now(),
    }

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
    return updated


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

    Finds the year's invoice, building and issuing it first when the student has not been
    billed yet - a parent at the window with cash should not be turned away because nobody
    pressed "generate" - then records the payment on it. A draft is issued rather than
    regenerated, so figures a clerk already quoted from it do not move under the payment.
    Refused, with the reason, when no fee structure applies (nothing to bill) or the amount
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
    if invoice is None or invoice.get("status") == InvoiceStatus.CANCELLED.value:
        invoice = generate_invoice(student_id, academic_year_id, actor_id, program)
    if invoice.get("status") == InvoiceStatus.DRAFT.value:
        invoice = issue_invoice(invoice, actor_id, program)

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
        "gateway_enabled": config["gateway_enabled"],
        "gateway_provider": config["gateway_provider"],
        "created_at": invoice.get("created_at"),
        "updated_at": invoice.get("updated_at"),
    }


def invoices_for_student(student_id) -> list[dict]:
    invoices = firestore_fee_invoices.query_documents("student_id", "==", int(student_id))
    return sorted(invoices, key=lambda i: str(i.get("academic_year_id") or ""), reverse=True)


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


def create_intent(invoice: dict, amount: float, actor_id: int | None = None,
                  instalment_label: str | None = None,
                  program: str = Program.LMS.value,
                  requested_method: str | None = None) -> dict:
    """
    Records an intention to pay, and hands back what the checkout page needs next.

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

    method = str(requested_method).upper() if requested_method else None
    offline = method in OFFLINE_GATEWAY_METHODS

    intent_id = firestore_payment_intents.get_next_numeric_id()
    document = {
        "invoice_id": invoice["id"],
        "invoice_number": invoice.get("invoice_number") or str(invoice["id"]),
        "student_id": int(invoice["student_id"]),
        "program": str(program).upper(),
        "amount": amount,
        "currency": invoice.get("currency") or config["currency"],
        "status": PaymentIntentStatus.CREATED.value,
        "provider": None if offline else config["gateway_provider"],
        "provider_reference": None,
        "checkout_url": None,
        "idempotency_key": uuid.uuid4().hex,
        "instalment_label": instalment_label,
        "requested_method": method,
        "provider_payload": {},
        "initiated_by": actor_id,
        "created_at": _now(),
    }
    firestore_payment_intents.add_document(str(intent_id), document)
    document["id"] = intent_id
    document["reference"] = intent_reference(intent_id)

    if offline:
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
            record_payment(
                invoice,
                type("GatewayPayment", (), {
                    "amount": intent["amount"],
                    "paid_at": None,
                    "method": PaymentMethod.ONLINE,
                    "reference": reference,
                    "instalment_label": intent.get("instalment_label"),
                    "note": f"Gateway payment via {intent.get('provider') or 'online'}",
                })(),
                actor_id=intent.get("initiated_by"),
            )
        logger.info("Intent %s succeeded; credited invoice %s.",
                    intent["id"], intent["invoice_id"])

    return settled


def intents_for_invoice(invoice_id) -> list[dict]:
    intents = firestore_payment_intents.query_documents("invoice_id", "==", invoice_id)
    for intent in intents:
        intent.setdefault("reference", intent_reference(intent.get("id")))
    return sorted(intents, key=lambda i: str(i.get("created_at") or ""), reverse=True)
