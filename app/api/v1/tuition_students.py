"""
Online tuition - student module.

What a tuition student can do: see the subjects they take and who teaches each, see their
timetable in their own timezone, join their class, do the work set for them, read their
attendance, and use the library.

Nothing here takes a `student_id`. Every route resolves to the caller, which is the only
scoping that cannot be got wrong by a client passing the wrong number - and in a product
where each arrangement is one family's private business, that matters more than the
convenience of a shared endpoint.
"""

import logging
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.v1.dependencies import require_tuition_student
from app.core.enums import InvoiceStatus, Program
from app.schemas.exam import ExamOut
from app.schemas.finance import PaymentIntentCreate, PaymentIntentOut
from app.schemas.tuition import (
    InvoiceOut, PackageStatusOut, StudentAttendanceReport, TuitionEnrollmentOut,
    TuitionSessionOut, TuitionSlotOut,
    TuitionFeeBreakdownOut,
)
from app.schemas.user import UserOut
from app.services import billing
from app.services.exams import hydrate_exam_for_student, prefetch_exams
from app.services.tuition import (
    assessments as assessment_service,
    enrollments as enrollment_service,
    fees as fee_service,
    reports as report_service,
    scheduling as schedule_service,
    sessions as session_service,
)

logger = logging.getLogger("tuition_student_api")

router = APIRouter(prefix="/tuition/students", tags=["Tuition - Student Module"])


# =======================================================================================
# My subjects and teachers
# =======================================================================================

@router.get("/my-subjects", response_model=List[TuitionEnrollmentOut])
def my_subjects(current_user: UserOut = Depends(require_tuition_student)):
    """
    [Tuition Student] The subjects I take, each with the teacher assigned to it.

    One teacher per subject - that is the shape of the product - and their details come back
    with the arrangement, along with the syllabus and the goals recorded for me.
    """
    records = enrollment_service.for_student(current_user.id)
    return [TuitionEnrollmentOut(**e) for e in enrollment_service.hydrate_many(records)]


# =======================================================================================
# My timetable
# =======================================================================================

@router.get("/timetable", response_model=List[TuitionSlotOut])
def my_timetable(current_user: UserOut = Depends(require_tuition_student)):
    """
    [Tuition Student] My recurring weekly class times.

    Different subjects fall on different days and at different times, and my timetable is
    guaranteed not to double-book me: a slot is refused if it overlaps one I already have,
    whichever teacher it belongs to.
    """
    records = schedule_service.slots_for_person(current_user.id, as_teacher=False)
    return [TuitionSlotOut(**s)
            for s in schedule_service.hydrate_many(schedule_service.sort_slots(records))]


@router.get("/sessions", response_model=List[TuitionSessionOut])
def my_sessions(
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    subject_id: int | None = Query(None),
    current_user: UserOut = Depends(require_tuition_student),
):
    """
    [Tuition Student] My classes, with every time shown in my own timezone.

    Each carries a `timing` block, including `minutes_remaining` counted to
    `effective_end_at` - the end of the class *after* any extension my teacher's late arrival
    earned me. That number comes from the server, so my screen and my teacher's cannot
    disagree about it.
    """
    records = session_service.list_for_person(current_user.id, as_teacher=False)
    records = session_service.filter_sessions(
        records, from_date, to_date, status_filter, subject_id
    )
    return [TuitionSessionOut(**s) for s in session_service.present_many(records, current_user)]


@router.get("/sessions/upcoming", response_model=List[TuitionSessionOut])
def my_upcoming(
    limit: int = Query(10, ge=1, le=50),
    current_user: UserOut = Depends(require_tuition_student),
):
    """[Tuition Student] My next few classes, across all my subjects."""
    from app.services.tuition.common import now_utc, to_utc

    moment = now_utc()
    records = [
        s for s in session_service.list_for_person(current_user.id, as_teacher=False)
        if s.get("status") in {"SCHEDULED", "IN_PROGRESS"}
        and (to_utc(s.get("scheduled_end_at")) or moment) >= moment
    ]
    records = session_service.sort_sessions(records)[:limit]
    return [TuitionSessionOut(**s) for s in session_service.present_many(records, current_user)]


@router.get("/sessions/{session_id}", response_model=TuitionSessionOut)
def get_session(session_id: str, current_user: UserOut = Depends(require_tuition_student)):
    record = session_service.require_session(session_id)
    session_service.assert_may_view(record, current_user)
    return TuitionSessionOut(**session_service.present(record, current_user))


@router.post("/sessions/{session_id}/join", response_model=TuitionSessionOut)
def join_class(session_id: str, current_user: UserOut = Depends(require_tuition_student)):
    """
    [Tuition Student] Record that I have joined, and get the meeting link.

    **I am not late for a class that has not started.** Lateness is measured from the moment
    the class actually began - my teacher pressing start - so joining at 17:05 for a class
    they open at 17:12 counts as on time, and only minutes after *that* count against me.
    `timing.waiting_for_teacher` says which of the two I am doing.

    Once it has started, joining late is recorded but changes nothing about when the class
    ends: my teacher may finish at the scheduled time, and the minutes I missed are mine.
    (The rule runs the other way for my teacher - if *they* are late, the class runs on so I
    still get a full lesson.)

    If the class has no link yet, one is generated now.
    """
    record = session_service.require_session(session_id)
    if record.get("student_id") != current_user.id:
        raise HTTPException(status_code=403, detail="This class is not yours to join.")

    if not record.get("meeting_link"):
        record = session_service.ensure_meeting_link(record, current_user)

    updated = session_service.student_join(record, current_user)
    return TuitionSessionOut(**session_service.present(updated, current_user))


# =======================================================================================
# My work
# =======================================================================================

@router.get("/assessments", response_model=List[ExamOut])
def my_work(
    category: str | None = Query(None, description="HOMEWORK | ASSIGNMENT | EXAM | TEST | PROJECT"),
    subject_id: int | None = Query(None),
    current_user: UserOut = Depends(require_tuition_student),
):
    """
    [Tuition Student] Homework, assignments and exams set for me, nearest deadline first.

    Answer keys are stripped: the student view of a paper never carries the correct answers,
    and explanations only appear once results are published. Drafts are not merely hidden -
    they cannot be opened even with the id.

    To sit a paper, save answers, upload a script or read a result, use the shared exam
    endpoints under `/exams/...` - the engine is the same one the school uses.
    """
    records = assessment_service.for_student(current_user.id)
    records = assessment_service.filter_assessments(records, category, subject_id)
    prefetch_exams(records)
    return [ExamOut(**hydrate_exam_for_student(r, current_user.id)) for r in records]


@router.get("/report-cards")
def my_report_cards(current_user: UserOut = Depends(require_tuition_student)):
    """
    [Tuition Student] My published report cards.

    Published only. A card a teacher has generated but not released is a working draft, and
    handing every draft to the family it is about would make the review step pointless.
    """
    return assessment_service.cards_for_student(current_user.id, published_only=True)


# =======================================================================================
# My attendance
# =======================================================================================

@router.get("/reports/me", response_model=StudentAttendanceReport)
def my_attendance_report(
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    current_user: UserOut = Depends(require_tuition_student),
):
    """
    [Tuition Student] My own attendance, broken down by subject.

    Mine only, and measured against classes that actually took place: a class my teacher
    missed does not count against me. Defaults to the last 30 days.
    """
    return report_service.student_report(current_user.id, from_date, to_date)


# =======================================================================================
# My fees
#
# The billing engine itself is admin-only and stays that way: generating, issuing, pricing
# and recording payment are the office's job. What was missing was the other half - a
# student could be billed and had no way to see it, which is the one thing every family
# asks for first.
#
# Both endpoints below are scoped to the caller and take no student id. There is nothing to
# tamper with: the invoice is looked up and then checked against `current_user.id`, so a
# guessed id answers 404 rather than somebody else's bill.
# =======================================================================================

@router.get("/package/me", response_model=PackageStatusOut)
def my_package(
    on: Optional[date] = Query(None, description="As of this date. Defaults to today."),
    current_user: UserOut = Depends(require_tuition_student),
):
    """
    [Tuition Student] The package I am on, and how many of its classes I have used this term.

    The header of the fee page: "Standard - 30 classes: 14 used, 16 remaining". Nulls, not
    an error, when nothing has been assigned yet.
    """
    return PackageStatusOut(**fee_service.package_status(current_user.id, on))


@router.get("/fees/me", response_model=TuitionFeeBreakdownOut)
def my_tuition_fees(
    period_start: date = Query(..., description="First day of the billing period."),
    period_end: date = Query(..., description="Last day of the billing period."),
    currency: Optional[str] = Query(
        None, description="Show in this currency, e.g. AED. Defaults to the base currency."
    ),
    current_user: UserOut = Depends(require_tuition_student),
):
    """
    [Tuition Student] **The payment page.** What I owe for a period, fully derived.

    Every line carries the classes it was priced from - "eight physics classes at 120" -
    followed by the discount, the tax and the convenience charge, and the total those add up
    to. The same computation the office's preview and the invoice generator use, so this
    cannot disagree with the bill that follows.

    Pass `currency` to switch; `available_currencies` is what the switcher should offer.
    Switching is more than a conversion - each currency carries its own tax and convenience
    charge, so the total moves by more than the exchange rate. `exchange_rate` and
    `base_currency` are returned so the page can show the conversion rather than leaving a
    payer wondering why the number changed.

    Nothing here is owed yet: it is what the period has accrued. The bill is the invoice.
    """
    return TuitionFeeBreakdownOut(**fee_service.compute_breakdown(
        current_user.id, period_start, period_end, currency=currency
    ))


@router.get("/invoices", response_model=List[InvoiceOut])
def my_invoices(
    status: str | None = Query(None, description="DRAFT, ISSUED, PARTIALLY_PAID, PAID, CANCELLED"),
    current_user: UserOut = Depends(require_tuition_student),
):
    """
    [Tuition Student] My tuition invoices, newest period first.

    Drafts are EXCLUDED. A draft is the office's working copy - it is re-priced every time
    it is regenerated and nothing on it is owed yet, so showing one to a student invites them
    to pay a figure that may still change. They appear the moment they are issued.
    """
    invoices = [
        invoice for invoice in fee_service.list_invoices(student_id=current_user.id, status=status)
        if invoice.get("status") != InvoiceStatus.DRAFT.value
    ]
    invoices.sort(key=lambda i: str(i.get("period_start") or ""), reverse=True)
    return [InvoiceOut(**fee_service.present_invoice(invoice)) for invoice in invoices]


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
def my_invoice(invoice_id: str, current_user: UserOut = Depends(require_tuition_student)):
    """
    [Tuition Student] One of my invoices, with its lines and payments.

    404 rather than 403 when it belongs to somebody else: a 403 confirms the invoice exists,
    which is more than a student needs to learn from guessing at ids.
    """
    invoice = fee_service.require_invoice(invoice_id)
    if int(invoice.get("student_id") or 0) != int(current_user.id):
        raise HTTPException(status_code=404, detail=f"Invoice {invoice_id} not found")
    if invoice.get("status") == InvoiceStatus.DRAFT.value:
        raise HTTPException(
            status_code=404,
            detail="That invoice has not been issued yet.",
        )
    return InvoiceOut(**fee_service.present_invoice(invoice))


# ---------------------------------------------------------------------------------------
# The checkout
#
# The same intent record the school's biller uses, on the tuition invoice. The student can
# only reach an invoice of their own - a 404, not a 403, for anybody else's - and the intent
# carries `program: TUITION` so a provider callback credits the right collection.
# ---------------------------------------------------------------------------------------

def _my_issued_invoice(invoice_id: str, student_id: int) -> dict:
    invoice = fee_service.require_invoice(invoice_id)
    if int(invoice.get("student_id") or 0) != int(student_id):
        raise HTTPException(status_code=404, detail=f"Invoice {invoice_id} not found")
    if invoice.get("status") == InvoiceStatus.DRAFT.value:
        raise HTTPException(status_code=404, detail="That invoice has not been issued yet.")
    return invoice


@router.post("/invoices/{invoice_id}/intents", response_model=PaymentIntentOut,
             status_code=201)
def start_my_payment(
    invoice_id: str, payload: PaymentIntentCreate,
    current_user: UserOut = Depends(require_tuition_student),
):
    """
    [Tuition Student] Start paying one of my invoices - the checkout page's "Pay" button.

    `method` is what I chose. For UPI, card, net banking or a wallet the intent waits for a
    gateway adapter and `checkout_url` says where to go (null while no provider is
    connected, with `detail` saying so). For a bank transfer or a payment at the office the
    response is the `reference` to quote; the office records the money and the invoice
    updates. Refused for more than is outstanding, and for a settled or cancelled invoice.
    """
    invoice = _my_issued_invoice(invoice_id, current_user.id)
    intent = billing.create_intent(
        invoice, payload.amount, current_user.id, None, Program.TUITION.value,
        getattr(payload.method, "value", payload.method),
    )
    return PaymentIntentOut(**intent)


@router.get("/invoices/{invoice_id}/intents", response_model=List[PaymentIntentOut])
def my_payment_attempts(invoice_id: str, current_user: UserOut = Depends(require_tuition_student)):
    """[Tuition Student] Every payment I have started on this invoice, newest first."""
    _my_issued_invoice(invoice_id, current_user.id)
    return [PaymentIntentOut(**i) for i in billing.intents_for_invoice(invoice_id)]
