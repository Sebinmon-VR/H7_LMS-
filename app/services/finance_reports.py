"""
School fee reports: who owes what, who has paid, and what came in.

Three views over the same records, because three different people ask:

  * the **student roll** - every student in the year with where their bill stands, billed
    or not. The office's working list.
  * the **dues report** - the roll narrowed to whoever still owes, with the overdue amount
    aged into buckets. The list somebody rings round from.
  * the **collections report** - every payment received in a period, by method, by day and
    by fee head, so the admission fees collected can be read apart from the tuition. What
    gets reconciled against the cash book and the bank. Opening-balance receipts - fees
    paid before this system existed, entered afterwards - are listed alongside, flagged by
    `source`, because the cash book is not complete without them.

Nothing here is stored. Each report is computed from the invoices and the roster at the
moment it is asked for, so it cannot disagree with the invoice screen - and a student who
has not been billed yet still appears, with what they *would* be billed, because "not on
the list" is how a fee gets forgotten.
"""

import csv
import io
from datetime import date, datetime

from fastapi import HTTPException

from app.core.enums import InstalmentStatus, InvoiceStatus, Program
from app.core.firebase import (
    firestore_classes, firestore_fee_heads, firestore_fee_invoices, firestore_fee_receipts,
    firestore_users,
)
from app.services import admissions as admission_service
from app.services import billing, finance
from app.services.finance import _as_date, money
from app.services.tuition.settings_store import currency_settings

NOT_BILLED = "NOT_BILLED"
OVERDUE = "OVERDUE"

AGEING_KEYS = ("not_due", "d0_30", "d31_60", "d61_90", "over_90")


def _zero_ageing() -> dict:
    return {key: 0.0 for key in AGEING_KEYS}


def _bucket(days_overdue: int) -> str:
    if days_overdue <= 0:
        return "not_due"
    if days_overdue <= 30:
        return "d0_30"
    if days_overdue <= 60:
        return "d31_60"
    if days_overdue <= 90:
        return "d61_90"
    return "over_90"


def _resolve_year(academic_year_id, program: str) -> dict:
    if academic_year_id is not None:
        return admission_service.require_year(academic_year_id)
    year = admission_service.current_year(program)
    if not year:
        raise HTTPException(
            status_code=400,
            detail="No academic year is configured. Create one before running fee reports.",
        )
    return year


def _live_invoices(year_id, program: str) -> dict[int, dict]:
    """The one live invoice per student for the year. A cancelled one is not a bill."""
    live: dict[int, dict] = {}
    for invoice in firestore_fee_invoices.list_all():
        if str(invoice.get("academic_year_id")) != str(year_id):
            continue
        if (invoice.get("program") or Program.LMS.value) != str(program).upper():
            continue
        if invoice.get("status") == InvoiceStatus.CANCELLED.value:
            continue
        live[int(invoice["student_id"])] = invoice
    return live


def _next_due(instalments: list[dict]) -> dict | None:
    """The instalment to chase next: the earliest-due one still owing."""
    owing = [
        i for i in instalments
        if i.get("status") not in (InstalmentStatus.PAID.value, InstalmentStatus.WAIVED.value)
        and money(i.get("outstanding", i.get("amount", 0))) > 0
    ]
    if not owing:
        return None
    owing.sort(key=lambda i: str(i.get("due_date") or "9999"))
    first = owing[0]
    return {
        "label": first.get("label"),
        "term": first.get("term"),
        "due_date": first.get("due_date"),
        "amount": money(first.get("outstanding", first.get("amount", 0))),
        "is_overdue": bool(first.get("is_overdue")),
        "days_overdue": int(first.get("days_overdue") or 0),
    }


def _last_payment(invoice: dict) -> dict | None:
    payments = [p for p in invoice.get("payments") or [] if p.get("paid_at")]
    if not payments:
        return None
    return max(payments, key=lambda p: str(p.get("paid_at")))


# ---------------------------------------------------------------------------------------
# The roll and the dues
# ---------------------------------------------------------------------------------------

def student_roll(academic_year_id=None, program: str = Program.LMS.value,
                 class_id=None, status: str | None = None, as_of: date | None = None,
                 include_expected: bool = True) -> dict:
    """
    Every student in the year with where their bill stands.

    A student with no invoice yet is listed as NOT_BILLED with `expected_total`, what the
    current rules would bill them, so the report answers "who is owed money" even before
    the office has pressed a button. `amount_due` is the figure to chase either way:
    the invoice's outstanding balance, or the expected total when nothing is billed.

    `status` narrows to one state - NOT_BILLED, DRAFT, ISSUED, PARTIALLY_PAID, PAID,
    OVERDUE - or DUE for everyone with anything left to pay.
    """
    year = _resolve_year(academic_year_id, program)
    today = as_of or date.today()
    currency = currency_settings(program)["base_currency"]

    roster = admission_service.students_in_year_detailed(
        year["id"], class_id, include_inactive=False, program=program,
    )
    invoices = _live_invoices(year["id"], program)

    rows = []
    for entry in roster:
        student_id = entry["student_id"]
        row = {
            "student_id": student_id,
            "full_name": entry.get("full_name"),
            "admission_number": entry.get("admission_number"),
            "roll_number": entry.get("roll_number"),
            "class_id": entry.get("class_id"),
            "class_name": entry.get("class_name"),
            "admission_category_name": entry.get("admission_category_name"),
            "status": NOT_BILLED,
            "invoice_id": None,
            "invoice_number": None,
            "currency": currency,
            "total_amount": 0.0,
            "amount_paid": 0.0,
            "amount_outstanding": 0.0,
            "amount_overdue": 0.0,
            "expected_total": None,
            "amount_due": 0.0,
            "is_overdue": False,
            "next_due": None,
            "last_payment_at": None,
            "last_payment_amount": None,
            "ageing": _zero_ageing(),
            "instalments": [],
        }

        invoice = invoices.get(student_id)
        if invoice:
            presented = billing.present_invoice(invoice, program, today)
            ageing = _zero_ageing()
            overdue_total = 0.0
            instalments = []
            for inst in presented["instalments"]:
                owing = money(inst.get("outstanding", money(inst.get("amount", 0)) - money(inst.get("amount_paid", 0))))
                if inst.get("status") in (InstalmentStatus.PAID.value, InstalmentStatus.WAIVED.value):
                    owing = 0.0
                days = int(inst.get("days_overdue") or 0) if inst.get("is_overdue") else 0
                if owing > 0:
                    ageing[_bucket(days)] = money(ageing[_bucket(days)] + owing)
                    if inst.get("is_overdue"):
                        overdue_total = money(overdue_total + owing)
                instalments.append({
                    "label": inst.get("label"),
                    "term": inst.get("term"),
                    "due_date": inst.get("due_date"),
                    "amount": money(inst.get("amount", 0)),
                    "amount_paid": money(inst.get("amount_paid", 0)),
                    "outstanding": owing,
                    "status": inst.get("status"),
                    "is_overdue": bool(inst.get("is_overdue")),
                    "days_overdue": days,
                })
            last = _last_payment(invoice)
            effective = presented["status"]
            if presented["is_overdue"] and effective in (
                InvoiceStatus.ISSUED.value, InvoiceStatus.PARTIALLY_PAID.value,
            ):
                effective = OVERDUE
            row.update({
                "status": effective,
                "invoice_id": presented["id"],
                "invoice_number": presented.get("invoice_number"),
                "currency": presented.get("currency") or currency,
                "total_amount": presented["total_amount"],
                "amount_paid": presented["amount_paid"],
                "amount_outstanding": presented["amount_outstanding"],
                "amount_overdue": overdue_total,
                "amount_due": presented["amount_outstanding"],
                "is_overdue": presented["is_overdue"],
                "next_due": _next_due(presented["instalments"]),
                "last_payment_at": last.get("paid_at") if last else None,
                "last_payment_amount": money(last.get("amount")) if last else None,
                "ageing": ageing,
                "instalments": instalments,
            })
        elif include_expected:
            student = firestore_users.get_document(str(student_id)) or {}
            try:
                breakdown = finance.compute_breakdown(student, year["id"], program, on=today)
                expected = money(breakdown["total_amount"])
            except HTTPException:
                expected = 0.0
            row["expected_total"] = expected
            row["amount_due"] = expected
            if expected > 0:
                row["ageing"]["not_due"] = expected

        rows.append(row)

    if status:
        wanted = status.strip().upper()
        if wanted == "DUE":
            rows = [r for r in rows if r["amount_due"] > 0]
        else:
            rows = [r for r in rows if r["status"] == wanted]

    return {
        "academic_year_id": int(year["id"]),
        "academic_year_name": year.get("name"),
        "as_of": today.isoformat(),
        "currency": currency,
        "class_id": int(class_id) if class_id is not None else None,
        "status": status,
        "summary": _roll_summary(rows, currency),
        "rows": rows,
    }


def _roll_summary(rows: list[dict], currency: str) -> dict:
    ageing = _zero_ageing()
    for row in rows:
        for key in AGEING_KEYS:
            ageing[key] = money(ageing[key] + row["ageing"].get(key, 0))
    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1
    billed = [r for r in rows if r["invoice_id"]]
    return {
        "currency": currency,
        "students": len(rows),
        "billed": len(billed),
        "not_billed": by_status.get(NOT_BILLED, 0),
        "paid": by_status.get(InvoiceStatus.PAID.value, 0),
        "partially_paid": by_status.get(InvoiceStatus.PARTIALLY_PAID.value, 0),
        "unpaid": by_status.get(InvoiceStatus.ISSUED.value, 0) + by_status.get(InvoiceStatus.DRAFT.value, 0),
        "overdue": by_status.get(OVERDUE, 0),
        "total_billed": money(sum(r["total_amount"] for r in billed)),
        "total_collected": money(sum(r["amount_paid"] for r in billed)),
        "total_outstanding": money(sum(r["amount_outstanding"] for r in billed)),
        "total_overdue": money(sum(r["amount_overdue"] for r in billed)),
        "expected_unbilled": money(sum(r["expected_total"] or 0 for r in rows if not r["invoice_id"])),
        "total_due": money(sum(r["amount_due"] for r in rows)),
        "by_status": by_status,
        "ageing": ageing,
    }


def dues_report(academic_year_id=None, program: str = Program.LMS.value,
                class_id=None, as_of: date | None = None) -> dict:
    """The roll narrowed to whoever still owes, largest balance first."""
    report = student_roll(academic_year_id, program, class_id, "DUE", as_of)
    report["rows"].sort(key=lambda r: (-r["amount_overdue"], -r["amount_due"], str(r["full_name"])))
    report["summary"] = _roll_summary(report["rows"], report["currency"])
    return report


# ---------------------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------------------

def _head_index() -> dict[int, dict]:
    """Every fee head by id, so a payment's share can be traced back to what it was for."""
    return {
        int(head["id"]): head
        for head in firestore_fee_heads.list_all()
        if head.get("id") is not None
    }


UNALLOCATED = "Unallocated"


def _allocate_payments(invoice: dict, heads: dict[int, dict]) -> list[list[dict]]:
    """
    Replays the invoice's own allocation rule one payment at a time.

    `billing._apply_payments_to_instalments` spreads the money over the schedule as a pool,
    which is all the invoice needs: what each line has left. A receipts report needs more -
    what *this* payment settled - so the same rule is run here per payment, in the same
    order (payments naming an instalment first, then the rest cascading from the earliest
    unpaid line), so the per-line totals agree with the invoice exactly. Within an
    instalment the amount is shared across its `components` in their proportions: a payment
    against Term 1 reads as tuition, one against the Admission Fee line reads as admission,
    which is the question this exists to answer. An instalment without components (a
    fixed-amount plan) falls back to the invoice's line items in their proportions.

    Returns one list per payment, in the invoice's payment order, each entry
    `{"instalment", "head", "fee_head_id", "is_admission_charge", "amount"}`.
    """
    payments = list(invoice.get("payments") or [])
    if not payments:
        return []

    line_items = invoice.get("line_items") or []
    item_by_name: dict = {}
    for item in line_items:
        item_by_name.setdefault(item.get("name"), item)

    def head_of(name) -> dict:
        item = item_by_name.get(name) or {}
        head_id = item.get("fee_head_id")
        head = heads.get(int(head_id), {}) if head_id is not None else {}
        return {
            "head": name or head.get("name") or "Fees",
            "fee_head_id": int(head_id) if head_id is not None else None,
            "is_admission_charge": bool(head.get("is_admission_charge")),
        }

    def split(label, amount: float, components: list) -> list[dict]:
        """Shares `amount` across the components in proportion; the last absorbs rounding."""
        parts = [c for c in components if money(c.get("amount")) > 0]
        if not parts:
            parts = [
                {"name": i.get("name"), "amount": money(i.get("net_amount", i.get("amount")))}
                for i in line_items
                if money(i.get("net_amount", i.get("amount"))) > 0
            ]
        if not parts:
            return [{"instalment": label, **head_of(None), "amount": money(amount)}]
        base = money(sum(money(p["amount"]) for p in parts))
        out, remaining = [], money(amount)
        for index, part in enumerate(parts):
            if index == len(parts) - 1:
                share = remaining
            else:
                share = money(amount * money(part["amount"]) / base) if base else 0.0
            remaining = money(remaining - share)
            if share:
                out.append({"instalment": label, **head_of(part.get("name")), "amount": share})
        return out

    schedule = []
    for line in invoice.get("instalments") or []:
        schedule.append({
            "label": line.get("label"),
            "amount": money(line.get("amount")),
            "paid": 0.0,
            "waived": line.get("status") == InstalmentStatus.WAIVED.value,
            "components": line.get("components") or [],
        })

    allocations: list[list[dict]] = [[] for _ in payments]

    if not schedule:
        for index, payment in enumerate(payments):
            allocations[index] = split(None, money(payment.get("amount")), [])
        return allocations

    by_label = {line["label"]: line for line in schedule}
    targeted, general = [], []
    for index, payment in enumerate(payments):
        (targeted if payment.get("instalment_label") else general).append(index)

    for index in targeted:
        payment = payments[index]
        line = by_label.get(payment.get("instalment_label"))
        if line is None:
            # The label no longer matches a line: the invoice treats it as general money.
            general.append(index)
            continue
        amount = money(payment.get("amount"))
        line["paid"] = money(line["paid"] + amount)
        allocations[index] = split(line["label"], amount, line["components"])

    for index in general:
        pool = money(payments[index].get("amount"))
        for line in schedule:
            if pool <= 0:
                break
            if line["waived"]:
                continue
            owing = money(line["amount"] - line["paid"])
            if owing <= 0:
                continue
            applied = min(owing, pool)
            line["paid"] = money(line["paid"] + applied)
            pool = money(pool - applied)
            allocations[index].extend(split(line["label"], applied, line["components"]))
        if pool > 0:
            # More money than the schedule: real, received, and not against any line.
            allocations[index].append({
                "instalment": None, "head": UNALLOCATED, "fee_head_id": None,
                "is_admission_charge": False, "amount": pool,
            })

    return allocations


def _merge_heads(allocation: list[dict]) -> list[dict]:
    """One entry per fee head for a payment, admission first, then largest first."""
    merged: dict = {}
    for part in allocation:
        key = (part["fee_head_id"], part["head"])
        entry = merged.setdefault(key, {
            "name": part["head"],
            "fee_head_id": part["fee_head_id"],
            "is_admission_charge": part["is_admission_charge"],
            "amount": 0.0,
        })
        entry["amount"] = money(entry["amount"] + part["amount"])
    return sorted(merged.values(), key=lambda h: (not h["is_admission_charge"], -h["amount"]))


def collections_report(from_date: date, to_date: date, academic_year_id=None,
                       program: str = Program.LMS.value, class_id=None,
                       method: str | None = None, head_id=None) -> dict:
    """
    Every payment received between two dates, newest first, with what it was against.

    Read from the invoices' own payment lists - the receipts side of the ledger - so a
    figure here always has an invoice behind it. Cash, cheque and transfer are the point;
    an ADJUSTMENT is included and labelled, since it settles a debt without being money and
    a cash book that hid it would not reconcile.

    Each payment is split by fee head (`heads`), replaying the invoice's allocation, so the
    admission fees collected (`admission_fee_amount` per row, `admission_fees` in the
    summary) can be read apart from the tuition. `head_id` keeps only the payments that
    settled that head, with the head's own total in `summary.head`.
    """
    if to_date < from_date:
        raise HTTPException(status_code=400, detail="to_date cannot be earlier than from_date.")
    currency = currency_settings(program)["base_currency"]
    wanted_method = method.strip().upper() if method else None
    wanted_head = int(head_id) if head_id is not None else None
    heads = _head_index()

    people: dict[int, dict] = {}

    def person(user_id) -> dict:
        if user_id is None:
            return {}
        if user_id not in people:
            people[user_id] = firestore_users.get_document(str(user_id)) or {}
        return people[user_id]

    classes: dict[int, tuple] = {}

    def class_of(student_id) -> tuple:
        if student_id not in classes:
            classes[student_id] = admission_service._class_of_student(student_id)
        return classes[student_id]

    def class_name_of(enrolled_class) -> str | None:
        if enrolled_class is None:
            return None
        return (firestore_classes.get_document(str(enrolled_class)) or {}).get("name")

    rows = []
    for invoice in firestore_fee_invoices.list_all():
        if (invoice.get("program") or Program.LMS.value) != str(program).upper():
            continue
        if academic_year_id is not None and str(invoice.get("academic_year_id")) != str(academic_year_id):
            continue
        student_id = int(invoice.get("student_id") or 0)
        enrolled_class, _ = class_of(student_id)
        if class_id is not None and enrolled_class != int(class_id):
            continue
        student = person(student_id)
        class_name = class_name_of(enrolled_class)

        allocations = _allocate_payments(invoice, heads)
        for index, payment in enumerate(invoice.get("payments") or []):
            paid_on = _as_date(payment.get("paid_at"))
            if paid_on is None or paid_on < from_date or paid_on > to_date:
                continue
            method_value = str(payment.get("method") or "OTHER").upper()
            if wanted_method and method_value != wanted_method:
                continue
            allocation = allocations[index] if index < len(allocations) else []
            split_heads = _merge_heads(allocation)
            if wanted_head is not None and not any(h["fee_head_id"] == wanted_head for h in split_heads):
                continue
            settled = []
            for part in allocation:
                if part["instalment"] and part["instalment"] not in settled:
                    settled.append(part["instalment"])
            clerk = person(payment.get("recorded_by"))
            rows.append({
                "paid_at": payment.get("paid_at"),
                "paid_on": paid_on.isoformat(),
                "amount": money(payment.get("amount")),
                "currency": invoice.get("currency") or currency,
                "method": method_value,
                "reference": payment.get("reference"),
                "note": payment.get("note"),
                "instalment_label": payment.get("instalment_label"),
                "instalments_settled": settled,
                "heads": split_heads,
                "admission_fee_amount": money(
                    sum(h["amount"] for h in split_heads if h["is_admission_charge"])
                ),
                "invoice_id": invoice.get("id"),
                "invoice_number": invoice.get("invoice_number"),
                "source": "INVOICE",
                "receipt_id": None,
                "academic_year_id": invoice.get("academic_year_id"),
                "student_id": student_id,
                "student_name": student.get("full_name"),
                "admission_number": student.get("admission_number"),
                "class_id": enrolled_class,
                "class_name": class_name,
                "recorded_by": payment.get("recorded_by"),
                "recorded_by_name": clerk.get("full_name"),
            })

    # Money with no invoice on this system: fees paid before it existed, entered afterwards
    # as opening balances. Real receipts, so they belong in the cash book with the rest -
    # flagged by `source` so a reconciliation never mistakes one for an invoice payment.
    for receipt in firestore_fee_receipts.list_all():
        if (receipt.get("program") or Program.LMS.value) != str(program).upper():
            continue
        if academic_year_id is not None and str(receipt.get("academic_year_id")) != str(academic_year_id):
            continue
        paid_on = _as_date(receipt.get("paid_at"))
        if paid_on is None or paid_on < from_date or paid_on > to_date:
            continue
        method_value = str(receipt.get("method") or "OTHER").upper()
        if wanted_method and method_value != wanted_method:
            continue
        head_id_value = int(receipt["fee_head_id"]) if receipt.get("fee_head_id") is not None else None
        if wanted_head is not None and head_id_value != wanted_head:
            continue
        student_id = int(receipt.get("student_id") or 0)
        enrolled_class, _ = class_of(student_id)
        if class_id is not None and enrolled_class != int(class_id):
            continue

        head = heads.get(head_id_value, {}) if head_id_value is not None else {}
        name = receipt.get("head_name") or head.get("name") or "Fees"
        is_admission = bool(receipt.get("is_admission_charge", head.get("is_admission_charge")))
        amount = money(receipt.get("amount"))
        student = person(student_id)
        clerk = person(receipt.get("recorded_by"))
        rows.append({
            "paid_at": receipt.get("paid_at"),
            "paid_on": paid_on.isoformat(),
            "amount": amount,
            "currency": receipt.get("currency") or currency,
            "method": method_value,
            "reference": receipt.get("reference"),
            "note": receipt.get("note"),
            "instalment_label": name,
            "instalments_settled": [],
            "heads": [{
                "name": name, "fee_head_id": head_id_value,
                "is_admission_charge": is_admission, "amount": amount,
            }],
            "admission_fee_amount": amount if is_admission else 0.0,
            "invoice_id": None,
            "invoice_number": None,
            "source": receipt.get("source") or "OPENING_BALANCE",
            "receipt_id": receipt.get("id"),
            "academic_year_id": receipt.get("academic_year_id"),
            "student_id": student_id,
            "student_name": student.get("full_name"),
            "admission_number": student.get("admission_number"),
            "class_id": enrolled_class,
            "class_name": class_name_of(enrolled_class),
            "recorded_by": receipt.get("recorded_by"),
            "recorded_by_name": clerk.get("full_name"),
        })

    rows.sort(key=lambda r: str(r["paid_at"]), reverse=True)

    by_method: dict[str, dict] = {}
    by_day: dict[str, dict] = {}
    by_class: dict[str, dict] = {}
    for row in rows:
        bucket = by_method.setdefault(row["method"], {"count": 0, "total": 0.0})
        bucket["count"] += 1
        bucket["total"] = money(bucket["total"] + row["amount"])
        day = by_day.setdefault(row["paid_on"], {"date": row["paid_on"], "count": 0, "total": 0.0})
        day["count"] += 1
        day["total"] = money(day["total"] + row["amount"])
        cls = by_class.setdefault(row["class_name"] or "No class", {"class_name": row["class_name"] or "No class", "count": 0, "total": 0.0})
        cls["count"] += 1
        cls["total"] = money(cls["total"] + row["amount"])

    # By fee head: the split the office actually asks for - "how much admission fee came in
    # this month" - which no per-payment total answers on its own.
    by_head: dict = {}
    for row in rows:
        for part in row["heads"]:
            key = (part["fee_head_id"], part["name"])
            bucket = by_head.setdefault(key, {
                "fee_head_id": part["fee_head_id"], "name": part["name"],
                "is_admission_charge": part["is_admission_charge"], "count": 0, "total": 0.0,
            })
            bucket["count"] += 1
            bucket["total"] = money(bucket["total"] + part["amount"])
    head_totals = sorted(by_head.values(), key=lambda b: (not b["is_admission_charge"], -b["total"]))
    total = money(sum(r["amount"] for r in rows))
    admission_fees = money(sum(r["admission_fee_amount"] for r in rows))
    chosen = None
    if wanted_head is not None:
        chosen = next((b for b in head_totals if b["fee_head_id"] == wanted_head), None) or {
            "fee_head_id": wanted_head,
            "name": (heads.get(wanted_head) or {}).get("name") or f"Head {wanted_head}",
            "is_admission_charge": bool((heads.get(wanted_head) or {}).get("is_admission_charge")),
            "count": 0, "total": 0.0,
        }

    return {
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "academic_year_id": int(academic_year_id) if academic_year_id is not None else None,
        "class_id": int(class_id) if class_id is not None else None,
        "method": wanted_method,
        "head_id": wanted_head,
        "currency": currency,
        "summary": {
            "currency": currency,
            "count": len(rows),
            "total": total,
            "admission_fees": admission_fees,
            "other_fees": money(total - admission_fees),
            "by_head": head_totals,
            "head": chosen,
            "by_method": by_method,
            "by_day": sorted(by_day.values(), key=lambda d: d["date"]),
            "by_class": sorted(by_class.values(), key=lambda c: -c["total"]),
        },
        "rows": rows,
    }


# ---------------------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------------------

def _cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _write(rows: list[list], header: list[str]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


ROLL_HEADER = [
    "Student ID", "Student", "Admission Number", "Roll Number", "Class", "Category", "Status",
    "Invoice Number", "Currency", "Invoice Total", "Paid", "Outstanding", "Overdue",
    "Expected (not billed)", "Amount Due", "Next Due", "Next Due Date", "Next Due Amount",
    "Last Payment Date", "Last Payment Amount",
    "Not Yet Due", "Overdue 0-30", "Overdue 31-60", "Overdue 61-90", "Overdue 90+",
]


def roll_csv(report: dict) -> str:
    rows = []
    for r in report["rows"]:
        nxt = r.get("next_due") or {}
        rows.append([_cell(v) for v in [
            r["student_id"], r["full_name"], r["admission_number"], r["roll_number"],
            r["class_name"], r["admission_category_name"], r["status"], r["invoice_number"],
            r["currency"], r["total_amount"], r["amount_paid"], r["amount_outstanding"],
            r["amount_overdue"], r["expected_total"], r["amount_due"],
            nxt.get("label"), nxt.get("due_date"), nxt.get("amount"),
            r["last_payment_at"], r["last_payment_amount"],
            r["ageing"]["not_due"], r["ageing"]["d0_30"], r["ageing"]["d31_60"],
            r["ageing"]["d61_90"], r["ageing"]["over_90"],
        ]])
    return _write(rows, ROLL_HEADER)


COLLECTIONS_HEADER = [
    "Paid At", "Date", "Student ID", "Student", "Admission Number", "Class", "Invoice Number",
    "Against", "Method", "Reference", "Currency", "Amount",
]
COLLECTIONS_TAIL = ["Recorded By", "Note", "Source"]


def collections_csv(report: dict) -> str:
    """
    One column per fee head after the amount - "Admission Fee", "Tuition Fee" - so the
    accountant can sum a column rather than parse a label. The heads come from the report's
    own totals, so a period with no admission fee has no empty column for it.
    """
    head_names = []
    for bucket in report["summary"].get("by_head", []):
        if bucket["name"] not in head_names:
            head_names.append(bucket["name"])
    rows = []
    for r in report["rows"]:
        portions: dict = {}
        for part in r.get("heads", []):
            portions[part["name"]] = money(portions.get(part["name"], 0.0) + part["amount"])
        against = r["instalment_label"] or ", ".join(r.get("instalments_settled") or [])
        rows.append([_cell(v) for v in [
            r["paid_at"], r["paid_on"], r["student_id"], r["student_name"], r["admission_number"],
            r["class_name"], r["invoice_number"], against, r["method"],
            r["reference"], r["currency"], r["amount"],
            *[portions.get(name, 0.0) for name in head_names],
            r["recorded_by_name"] or r["recorded_by"], r["note"], r.get("source"),
        ]])
    return _write(rows, COLLECTIONS_HEADER + head_names + COLLECTIONS_TAIL)
