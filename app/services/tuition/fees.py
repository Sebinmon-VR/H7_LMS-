"""
Tuition fees: what a class costs, and what a student owes.

Admin-only, in every direction. Teachers and students never read this module - it is not
exposed on their routers at all - because a teacher seeing what a student is charged, or a
student seeing what a teacher is paid, is a conversation nobody asked this system to start.

The design follows directly from the brief's own sentence: *"how many classes a student and
teacher attended, based on that count we are going to plan the fees"*. So an invoice is not
typed in, it is **derived** - `app.services.tuition.reports` counts the conducted classes and
this module prices them. The counts are stored on the invoice line alongside the price, which
is what makes a bill explicable three months later: an invoice can always be read back as
"eight physics classes at 120".

Fee plans resolve most-specific-first - the enrollment's own plan, then a plan for that
subject, then the programme default. That ordering lets a programme set one rate and override
it for the one student who negotiated a different one, without a plan per enrollment.

A DRAFT invoice is recomputed from the sessions every time it is regenerated. Once ISSUED the
numbers are frozen, because a bill that silently changes after it was sent is not a bill.
"""

import logging
from datetime import date, datetime

from fastapi import HTTPException

from app.core.enums import FeeBasis, InvoiceStatus
from app.core.firebase import (
    firestore_subjects, firestore_tuition_fee_plans, firestore_tuition_invoices,
    firestore_users,
)
from app.services.tuition.common import now_utc, parse_date, store_dt, user_id_of
from app.services.tuition.enrollments import for_student
from app.services.tuition.reports import enrollment_counts
from app.services.tuition.settings_store import tuition_settings

logger = logging.getLogger("tuition.fees")


# ---------------------------------------------------------------------------------------
# Fee plans
# ---------------------------------------------------------------------------------------

def require_plan(plan_id) -> dict:
    plan = firestore_tuition_fee_plans.get_document(str(plan_id))
    if not plan:
        raise HTTPException(status_code=404, detail=f"Fee plan {plan_id} not found")
    return plan


def create_plan(payload, actor) -> dict:
    if payload.subject_id is not None:
        if not firestore_subjects.get_document(str(payload.subject_id)):
            raise HTTPException(status_code=404, detail=f"Subject {payload.subject_id} not found")

    plan_id = firestore_tuition_fee_plans.get_next_numeric_id()
    document = {
        "name": payload.name,
        "basis": payload.basis.value,
        "amount": float(payload.amount),
        "currency": payload.currency or tuition_settings()["currency"],
        "subject_id": payload.subject_id,
        "no_show_amount": payload.no_show_amount,
        "charge_teacher_no_show": payload.charge_teacher_no_show,
        "is_active": payload.is_active,
        "notes": payload.notes,
        "created_by": user_id_of(actor),
        "created_at": datetime.utcnow().isoformat(),
    }
    firestore_tuition_fee_plans.add_document(str(plan_id), document)
    document["id"] = plan_id
    return document


def update_plan(plan: dict, payload) -> dict:
    changed = payload.model_dump(exclude_unset=True)
    updates = {}
    for field in ("name", "amount", "currency", "subject_id", "no_show_amount",
                  "charge_teacher_no_show", "is_active", "notes"):
        if field in changed:
            updates[field] = changed[field]
    if "basis" in changed and changed["basis"] is not None:
        value = changed["basis"]
        updates["basis"] = value.value if hasattr(value, "value") else str(value)
    if not updates:
        return plan
    updates["updated_at"] = datetime.utcnow().isoformat()
    firestore_tuition_fee_plans.add_document(str(plan["id"]), updates)
    return {**plan, **updates}


def resolve_plan(enrollment: dict) -> dict:
    """
    The plan that prices one arrangement: its own, then its subject's, then the default.

    The synthesized fallback is a real plan document in shape but has no `id`, so the caller
    can price a programme that has never configured fees at all - it simply produces zero-
    valued lines with the session counts still filled in, which is exactly the report an
    admin wants while they are deciding what to charge.
    """
    config = tuition_settings()

    plan_id = enrollment.get("fee_plan_id")
    if plan_id is not None:
        plan = firestore_tuition_fee_plans.get_document(str(plan_id))
        if plan and plan.get("is_active", True):
            return plan

    subject_id = enrollment.get("subject_id")
    candidates = [
        p for p in firestore_tuition_fee_plans.query_documents("subject_id", "==", subject_id)
        if p.get("is_active", True)
    ]
    if candidates:
        return candidates[0]

    programme_wide = [
        p for p in firestore_tuition_fee_plans.query_documents("subject_id", "==", None)
        if p.get("is_active", True)
    ]
    if programme_wide:
        return programme_wide[0]

    return {
        "id": None,
        "name": "Programme default",
        "basis": FeeBasis.PER_SESSION.value,
        "amount": config["default_session_fee"],
        "currency": config["currency"],
        "no_show_amount": None,
        "charge_teacher_no_show": False,
    }


# ---------------------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------------------

def price_enrollment(enrollment: dict, period_start: date, period_end: date) -> dict:
    """
    Prices one arrangement over a period, returning the line with its workings attached.

    The three bases charge different things, and each is a real arrangement somebody runs:

      * PER_SESSION - a rate per class held. The default, and what the brief describes.
      * HOURLY      - the minutes actually taught. This is where the timing rules become
                      money: a class extended because the teacher was late bills for the
                      longer lesson the student actually received.
      * MONTHLY     - a flat retainer. The session counts are still reported, because an
                      admin comparing "paid for a month, got three classes" is exactly the
                      conversation a retainer produces.
    """
    counts = enrollment_counts(enrollment["id"], period_start, period_end)
    plan = resolve_plan(enrollment)
    basis = plan.get("basis", FeeBasis.PER_SESSION.value)
    unit = float(plan.get("amount") or 0)

    billable = counts["billable_sessions"]
    missed = counts["missed"]

    if basis == FeeBasis.MONTHLY.value:
        amount = unit if counts["conducted"] else 0.0
        quantity = 1 if counts["conducted"] else 0
        unit_label = "month"
    elif basis == FeeBasis.HOURLY.value:
        hours = counts["taught_minutes"] / 60.0
        amount = round(unit * hours, 2)
        quantity = round(hours, 2)
        unit_label = "hour"
    else:
        # A missed class may be charged at a reduced rate; `no_show_amount` unset means the
        # student is billed in full for a class their teacher turned up to.
        no_show_rate = plan.get("no_show_amount")
        held = billable - missed
        amount = unit * max(held, 0)
        if missed:
            amount += (unit if no_show_rate is None else float(no_show_rate)) * missed
        quantity = billable
        unit_label = "class"

    subject = firestore_subjects.get_document(str(enrollment.get("subject_id"))) or {}
    teacher = firestore_users.get_document(str(enrollment.get("teacher_id"))) or {}

    return {
        "enrollment_id": enrollment["id"],
        "subject_id": enrollment.get("subject_id"),
        "subject_name": subject.get("name"),
        "teacher_id": enrollment.get("teacher_id"),
        "teacher_name": teacher.get("full_name"),
        "fee_plan_id": plan.get("id"),
        "fee_plan_name": plan.get("name"),
        "basis": basis,
        "unit_amount": unit,
        "unit_label": unit_label,
        "quantity": quantity,
        # The counts the price came from, carried on the line so the bill explains itself.
        "sessions_conducted": counts["conducted"],
        "sessions_billable": billable,
        "sessions_attended": counts["attended"],
        "sessions_missed": missed,
        "sessions_cancelled": counts["cancelled"],
        "teacher_no_show": counts["teacher_no_show"],
        "taught_minutes": counts["taught_minutes"],
        "amount": round(amount, 2),
    }


# ---------------------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------------------

def invoice_id(student_id, period_start: date, period_end: date) -> str:
    """
    Derived from student and period, so regenerating a month corrects the bill rather than
    issuing a second one for the same classes - the way duplicate invoices actually happen.
    """
    return f"{student_id}_{period_start.isoformat()}_{period_end.isoformat()}"


def require_invoice(invoice_id_value) -> dict:
    invoice = firestore_tuition_invoices.get_document(str(invoice_id_value))
    if not invoice:
        raise HTTPException(status_code=404, detail=f"Invoice {invoice_id_value} not found")
    return invoice


def generate_invoice(student_id: int, period_start, period_end, actor,
                     discount_amount: float = 0.0, tax_amount: float = 0.0,
                     due_date=None, notes: str | None = None) -> dict:
    """
    Builds (or rebuilds) a student's bill for a period from their conducted classes.

    Refuses to overwrite an invoice that has already been issued. That is the one hard rule
    in this module: regenerating a draft is routine housekeeping, silently restating a bill
    somebody has already been sent is not, and the two are one careless button apart.
    """
    start = parse_date(period_start)
    end = parse_date(period_end)
    if not start or not end:
        raise HTTPException(status_code=400, detail="period_start and period_end are required.")
    if end < start:
        raise HTTPException(status_code=400, detail="period_end cannot be earlier than period_start.")

    student = firestore_users.get_document(str(student_id))
    if not student:
        raise HTTPException(status_code=404, detail=f"Student {student_id} not found")

    document_id = invoice_id(student_id, start, end)
    existing = firestore_tuition_invoices.get_document(document_id)
    if existing and existing.get("status") != InvoiceStatus.DRAFT.value:
        raise HTTPException(
            status_code=409,
            detail=f"Invoice {document_id} is already {existing.get('status')} and cannot be "
                   f"regenerated. Cancel it first if the figures are wrong.",
        )

    config = tuition_settings()
    lines = [
        price_enrollment(enrollment, start, end)
        for enrollment in for_student(student_id, include_inactive=True)
    ]
    # Arrangements with nothing in the period are dropped from the bill but the fact that
    # they were considered is not interesting enough to keep as a zero line.
    lines = [line for line in lines if line["sessions_conducted"] or line["amount"]]

    subtotal = round(sum(line["amount"] for line in lines), 2)
    total = round(subtotal - float(discount_amount or 0) + float(tax_amount or 0), 2)

    document = {
        "student_id": student_id,
        "student_name": student.get("full_name"),
        "admission_number": student.get("admission_number"),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "status": InvoiceStatus.DRAFT.value,
        "currency": config["currency"],
        "line_items": lines,
        "subtotal": subtotal,
        "discount_amount": float(discount_amount or 0),
        "tax_amount": float(tax_amount or 0),
        "total_amount": total,
        "amount_paid": float((existing or {}).get("amount_paid") or 0),
        "payments": (existing or {}).get("payments") or [],
        "due_date": parse_date(due_date).isoformat() if parse_date(due_date) else None,
        "notes": notes,
        "generated_by": user_id_of(actor),
        "created_at": (existing or {}).get("created_at") or datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
    }
    firestore_tuition_invoices.add_document(document_id, document)
    document["id"] = document_id
    return document


def generate_batch(period_start, period_end, actor) -> dict:
    """
    Bills every student with classes in a period.

    An already-issued invoice is skipped and reported rather than failing the run: the point
    of a monthly batch is that it can be re-run after fixing one student's data without the
    admin having to work out which of sixty students it got to last time.
    """
    start = parse_date(period_start)
    end = parse_date(period_end)
    from app.services.tuition.reports import programme_report

    report = programme_report(start, end)
    generated, skipped = [], []

    for row in report["students"]:
        try:
            invoice = generate_invoice(row["student_id"], start, end, actor)
            generated.append({"student_id": row["student_id"], "invoice_id": invoice["id"],
                              "total_amount": invoice["total_amount"]})
        except HTTPException as exc:
            skipped.append({"student_id": row["student_id"], "reason": str(exc.detail)})

    return {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "generated": generated,
        "skipped": skipped,
        "total_billed": round(sum(row["total_amount"] for row in generated), 2),
    }


def _settlement_status(paid: float, total: float, fallback: str) -> str:
    """
    Where an issued invoice stands against what has been paid.

    Derived on every write rather than stored independently, so the status can never drift
    out of step with the payments beneath it - the classic way an invoice ends up marked
    unpaid with three receipts attached.
    """
    if paid <= 0:
        return fallback
    if total > 0 and paid >= total:
        return InvoiceStatus.PAID.value
    return InvoiceStatus.PARTIALLY_PAID.value


def issue_invoice(invoice: dict, actor) -> dict:
    """
    Freezes a draft and marks it sent.

    Issuing an invoice already settled in advance is normal and lands straight on PAID: a
    student who paid up front does not need the bill to pretend otherwise for a moment
    first. What cannot happen is issuing something already issued - the figures are frozen
    at that point, and re-issuing would be a second bill for the same classes.
    """
    if invoice.get("status") != InvoiceStatus.DRAFT.value:
        raise HTTPException(status_code=409, detail=f"Invoice is already {invoice.get('status')}.")

    paid = float(invoice.get("amount_paid") or 0)
    total = float(invoice.get("total_amount") or 0)

    updates = {
        "status": _settlement_status(paid, total, InvoiceStatus.ISSUED.value),
        "issued_at": store_dt(now_utc()),
        "issued_by": user_id_of(actor),
        "updated_at": datetime.utcnow().isoformat(),
    }
    firestore_tuition_invoices.add_document(str(invoice["id"]), updates)
    return {**invoice, **updates}


def record_payment(invoice: dict, actor, amount: float, method: str | None = None,
                   reference: str | None = None, paid_at=None) -> dict:
    """
    Records money received and moves the invoice's status to match.

    Payments are appended rather than summed into a single field, so an invoice settled in
    three instalments shows three instalments. The status is derived from the running total
    on every write, which means it cannot drift out of step with the payments beneath it.
    """
    if invoice.get("status") == InvoiceStatus.CANCELLED.value:
        raise HTTPException(status_code=409, detail="A cancelled invoice cannot take payments.")
    if amount is None or float(amount) <= 0:
        raise HTTPException(status_code=400, detail="Payment amount must be greater than zero.")

    payments = list(invoice.get("payments") or [])
    payments.append({
        "amount": round(float(amount), 2),
        "paid_at": store_dt(paid_at or now_utc()),
        "method": method,
        "reference": reference,
        "recorded_by": user_id_of(actor),
    })

    paid = round(sum(float(p.get("amount") or 0) for p in payments), 2)
    total = float(invoice.get("total_amount") or 0)

    # A draft stays a draft, however much has been paid against it. Money received before
    # the bill was sent is an advance, not a settlement, and moving the invoice out of DRAFT
    # here would quietly do two wrong things: block the admin from regenerating the figures
    # after correcting a class, and block them from ever issuing it - a draft is the only
    # thing `issue_invoice` accepts. The payment is recorded either way and is applied the
    # moment the invoice is issued.
    if invoice.get("status") == InvoiceStatus.DRAFT.value:
        status = InvoiceStatus.DRAFT.value
    else:
        status = _settlement_status(paid, total, str(invoice.get("status")))

    updates = {
        "payments": payments,
        "amount_paid": paid,
        "status": status,
        "updated_at": datetime.utcnow().isoformat(),
    }
    firestore_tuition_invoices.add_document(str(invoice["id"]), updates)
    return {**invoice, **updates}


def cancel_invoice(invoice: dict, reason: str | None = None) -> dict:
    updates = {
        "status": InvoiceStatus.CANCELLED.value,
        "cancellation_reason": reason,
        "updated_at": datetime.utcnow().isoformat(),
    }
    firestore_tuition_invoices.add_document(str(invoice["id"]), updates)
    return {**invoice, **updates}


def list_invoices(student_id: int | None = None, status: str | None = None,
                  from_date=None, to_date=None) -> list[dict]:
    records = (
        firestore_tuition_invoices.query_documents("student_id", "==", student_id)
        if student_id is not None else firestore_tuition_invoices.list_all()
    )
    start = parse_date(from_date)
    end = parse_date(to_date)

    results = []
    for record in records:
        if status and record.get("status") != status:
            continue
        period_start = parse_date(record.get("period_start"))
        if start and period_start and period_start < start:
            continue
        if end and period_start and period_start > end:
            continue
        results.append(record)

    return sorted(results, key=lambda r: str(r.get("period_start") or ""), reverse=True)


def revenue_summary(from_date=None, to_date=None) -> dict:
    """
    Billed against collected over a period. The admin's one-line answer to 'how are we doing'.
    """
    invoices = list_invoices(from_date=from_date, to_date=to_date)
    live = [i for i in invoices if i.get("status") != InvoiceStatus.CANCELLED.value]
    billed = round(sum(float(i.get("total_amount") or 0) for i in live), 2)
    collected = round(sum(float(i.get("amount_paid") or 0) for i in live), 2)

    by_status: dict[str, int] = {}
    for invoice in invoices:
        by_status[str(invoice.get("status"))] = by_status.get(str(invoice.get("status")), 0) + 1

    return {
        "currency": tuition_settings()["currency"],
        "invoice_count": len(invoices),
        "total_billed": billed,
        "total_collected": collected,
        "outstanding": round(billed - collected, 2),
        "by_status": by_status,
    }
