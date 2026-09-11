"""
Online tuition - administrator module.

Everything an administrator does that a teacher or student cannot: mapping teachers to
students, building the timetable, seeing the whole programme's numbers, and the fee module,
which is admin-only in every direction and appears on no other router.

The route prefix is `/admin/tuition` rather than a second top-level namespace, so an
administrator's existing token and existing navigation reach it without a separate sign-in -
which is what the brief asks for: the same admin, with a separate set of options.
"""

import logging
from datetime import date, datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.api.v1.dependencies import require_admin
from app.core.enums import (
    Program, TEACHING_ROLE_VALUES, UserRole, normalize_programs,
)
from app.core.firebase import (
    firestore_tuition_enrollments, firestore_tuition_sessions, firestore_tuition_slots,
    firestore_users, require_document,
)
from app.schemas.tuition import (
    FeePlanCreate, FeePlanOut, FeePlanUpdate, InvoiceBatchGenerate, InvoiceGenerate,
    InvoiceOut, MeetingLinkUpdate, PaymentRecord, ProgramAccessUpdate, ProgramSettingsUpdate,
    ProgrammeReport, SlotAvailabilityQuery, SlotAvailabilityResult, StudentAttendanceReport,
    TeacherAttendanceReport, TuitionEnrollmentCreate, TuitionEnrollmentOut,
    TuitionEnrollmentUpdate, TuitionSessionCancel, TuitionSessionCreate, TuitionSessionOut,
    TuitionSessionReschedule, TuitionSlotCreate, TuitionSlotOut, TuitionSlotUpdate,
    TuitionStudentCreate, TuitionTeacherCreate, TuitionUserSummary,
)
from app.schemas.user import UserOut
from app.services import accounts as account_service
from app.services.tuition import (
    enrollments as enrollment_service,
    exports as export_service,
    fees as fee_service,
    reminders as reminder_service,
    reports as report_service,
    scheduling as schedule_service,
    sessions as session_service,
)
from app.services.tuition.settings_store import program_settings, save_settings

logger = logging.getLogger("tuition_admin_api")

router = APIRouter(prefix="/admin/tuition", tags=["Tuition - Admin Module"])


# =======================================================================================
# Programme settings
# =======================================================================================

@router.get("/settings/{program}")
def get_program_settings(program: Program, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] The effective configuration for a product - LMS or TUITION.

    "Effective" is the useful word: it merges what the admin has saved over what the
    environment configures over the built-in defaults, so what comes back is what the system
    will actually do, not what somebody once typed into a form.
    """
    return program_settings(program.value)


@router.put("/settings/{program}")
def update_program_settings(
    program: Program,
    payload: ProgramSettingsUpdate,
    current_user: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Change reminder lead times, the timezone, class length and the timing rules.

    Covers the brief's request to set the reminder time "for both the lms and tuition":
    `PUT /admin/tuition/settings/LMS` changes the school timetable's reminders and
    `.../TUITION` the one-to-one classes', with no redeploy either way.

    Merges rather than replaces - an admin screen that renders half these fields must not
    silently reset the other half - and returns the full effective settings so the caller
    sees exactly what took effect.
    """
    try:
        return save_settings(program.value, payload.model_dump(exclude_unset=True), current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# =======================================================================================
# Programme membership
# =======================================================================================

@router.post("/students", response_model=TuitionUserSummary,
             status_code=status.HTTP_201_CREATED)
def add_tuition_student(payload: TuitionStudentCreate,
                        _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Add a **new student** to the tuition programme.

    Creates a fresh account - login, profile and tuition access in one call. Tuition students
    are not the school's pupils: the two user bases overlap only sometimes, and requiring an
    LMS profile to exist first would mean every tuition family had to be enrolled in a school
    they may have nothing to do with. Tick `also_lms` for somebody who genuinely is both.

    - `email` may be omitted and is derived as firstname.lastname@USER_EMAIL_DOMAIN.
    - `password` may be omitted; issue one later with
      `POST /admin/users/{id}/generate-credentials`, which emails it to them.
    - `admission_number` is generated as `TUI-<year>-0001` when omitted, so every student
      carries a unique id without an administrator having to invent one.
    - `timezone` may be omitted: it is detected from the student's browser on their first
      request and saved, which is what makes a student outside India see their own local time.

    To attach subjects and teachers afterwards, create enrollments.
    """
    account = account_service.create_tuition_account(payload, UserRole.STUDENT)
    return TuitionUserSummary(**account)


@router.post("/teachers", response_model=TuitionUserSummary,
             status_code=status.HTTP_201_CREATED)
def add_tuition_teacher(payload: TuitionTeacherCreate,
                        _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Add a **new teacher** to the tuition programme.

    Same reasoning as adding a student: a tuition teacher need not be one of the school's
    staff. `employee_id` is generated as `TUT-<year>-0001` when omitted.

    `subject_ids` records what this teacher can take, so the enrollment screen can offer the
    right candidates. It assigns nobody on its own - the assignment *is* the enrollment, and
    duplicating it here would give two answers to "who teaches this?".
    """
    account = account_service.create_tuition_account(payload, UserRole.TEACHER)
    return TuitionUserSummary(**account)


@router.get("/users", response_model=List[TuitionUserSummary])
def list_tuition_users(
    role: UserRole | None = Query(None, description="Filter to STUDENT or TEACHER"),
    include_all: bool = Query(
        False, description="Include accounts without tuition access, for granting it"
    ),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] The people in the tuition programme.

    `include_all=true` is how an admin finds an existing school account to grant tuition
    access to, rather than creating a duplicate person with a second login.
    """
    people = []
    for user in firestore_users.list_all():
        programs = normalize_programs(user.get("programs"))
        if not include_all and Program.TUITION.value not in programs:
            continue
        if role is not None:
            wanted = TEACHING_ROLE_VALUES if role in TEACHING_ROLE_VALUES else {role.value}
            if user.get("role") not in wanted:
                continue
        people.append({**user, "programs": programs})

    people.sort(key=lambda u: str(u.get("full_name") or ""))
    return [TuitionUserSummary(**person) for person in people]


@router.put("/users/{user_id}/programs", response_model=TuitionUserSummary)
def set_user_programs(
    user_id: int,
    payload: ProgramAccessUpdate,
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Grant or revoke a user's access to a product.

    This is the "special key" the brief describes. A teacher or student holding TUITION
    reaches the tuition modules and, unless they also hold LMS, nothing else - their role
    still decides what they may do, but this decides where.

    Replaces the list outright rather than merging, because access an admin cannot take away
    is not access control.
    """
    user = require_document(firestore_users, user_id, "User")
    programs = normalize_programs([p.value for p in payload.programs])

    firestore_users.add_document(str(user_id), {
        "programs": programs,
        "updated_at": datetime.utcnow().isoformat(),
    })
    logger.info("User %s programme access set to %s.", user_id, programs)
    return TuitionUserSummary(**{**user, "programs": programs})


# =======================================================================================
# Enrollments - the spine of the product
# =======================================================================================

@router.post("/enrollments", response_model=TuitionEnrollmentOut,
             status_code=status.HTTP_201_CREATED)
def create_enrollment(payload: TuitionEnrollmentCreate,
                      current_user: UserOut = Depends(require_admin)):
    """
    [Admin Only] Assign a teacher to a student for one subject.

    The core mapping the brief describes: a student opts into a subject and one teacher is
    assigned to it. Refused with 409 if that student already has an active teacher for that
    subject - one subject has one teacher, so a second would leave "who marks this?"
    unanswerable.

    Both participants must hold tuition access and the right role, and the error says which
    check failed so an admin knows whether to change the person or tick a box.
    """
    record = enrollment_service.create_enrollment(payload, current_user)
    return TuitionEnrollmentOut(**enrollment_service.hydrate_many([record])[0])


@router.get("/enrollments", response_model=List[TuitionEnrollmentOut])
def list_enrollments(
    student_id: int | None = Query(None),
    teacher_id: int | None = Query(None),
    subject_id: int | None = Query(None),
    include_inactive: bool = Query(False, description="Include paused, completed and cancelled"),
    _: UserOut = Depends(require_admin),
):
    """[Admin Only] Every tuition arrangement, with the student's and teacher's full details."""
    if student_id is not None:
        records = enrollment_service.for_student(student_id, include_inactive)
    elif teacher_id is not None:
        records = enrollment_service.for_teacher(teacher_id, include_inactive)
    else:
        records = firestore_tuition_enrollments.list_all()
        if not include_inactive:
            records = [
                r for r in records if r.get("status") in enrollment_service.ACTIVE_STATUSES
            ]

    if subject_id is not None:
        records = [r for r in records if r.get("subject_id") == subject_id]

    return [TuitionEnrollmentOut(**r) for r in enrollment_service.hydrate_many(records)]


@router.get("/enrollments/{enrollment_id}", response_model=TuitionEnrollmentOut)
def get_enrollment(enrollment_id: int, _: UserOut = Depends(require_admin)):
    record = enrollment_service.require_enrollment(enrollment_id)
    return TuitionEnrollmentOut(**enrollment_service.hydrate_many([record])[0])


@router.put("/enrollments/{enrollment_id}", response_model=TuitionEnrollmentOut)
def update_enrollment(
    enrollment_id: int,
    payload: TuitionEnrollmentUpdate,
    current_user: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Edit an arrangement, including moving it to a different teacher.

    A teacher change cascades: the recurring slots and every class still to come are
    re-pointed at the new teacher, while classes already taught keep the teacher who actually
    took them. If the new teacher is busy at one of those times the move still goes through
    and the clash appears on `GET /admin/tuition/conflicts` - blocking the reassignment would
    leave the subject pointing at a teacher who has already left.
    """
    record = enrollment_service.require_enrollment(enrollment_id)
    updated = enrollment_service.apply_update(record, payload, current_user)
    return TuitionEnrollmentOut(**enrollment_service.hydrate_many([updated])[0])


@router.delete("/enrollments/{enrollment_id}")
def delete_enrollment(enrollment_id: int, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Remove an arrangement and the schedule hanging off it.

    Ending it - `PUT` with `status: COMPLETED` - is almost always what you want instead: it
    keeps the history, the materials and the invoices explicable. This exists for the record
    created in error. Classes already taught are never deleted, whichever you use.
    """
    enrollment_service.require_enrollment(enrollment_id)
    removed = enrollment_service.delete_enrollment(enrollment_id)
    return {"enrollment_id": enrollment_id, "removed": removed,
            "detail": "Enrollment deleted. Past classes were kept as a record of teaching."}


# =======================================================================================
# The timetable
# =======================================================================================

@router.post("/slots", response_model=TuitionSlotOut, status_code=status.HTTP_201_CREATED)
def create_slot(
    payload: TuitionSlotCreate,
    allow_conflicts: bool = Query(
        False,
        description="Book anyway despite clashes. The conflicts are still returned and "
                    "recorded, so an override is a decision somebody made rather than a "
                    "silent one.",
    ),
    current_user: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Book a recurring weekly class time.

    Checked against **both** diaries. A school timetable clashes when two periods want the
    same teacher; a tuition timetable clashes when two classes want the same teacher *or the
    same student* - and since a student's two teachers have no way of knowing about each
    other, nothing but this check stands between them and a double booking.

    Returns 409 with a list naming who is busy and when. On success the classes this rule
    implies are generated immediately, so the student and teacher see the new timetable at
    once rather than after the next sweep.
    """
    record = schedule_service.create_slot(payload, current_user, allow_conflicts)
    hydrated = schedule_service.hydrate_many([record])[0]
    return TuitionSlotOut(**{**hydrated, **{k: record[k] for k in
                                            ("conflicts", "sessions_generated") if k in record}})


@router.get("/slots", response_model=List[TuitionSlotOut])
def list_slots(
    enrollment_id: int | None = Query(None),
    student_id: int | None = Query(None),
    teacher_id: int | None = Query(None),
    _: UserOut = Depends(require_admin),
):
    """[Admin Only] The recurring timetable, filtered by arrangement, student or teacher."""
    if enrollment_id is not None:
        records = schedule_service.slots_for_enrollment(enrollment_id)
    elif teacher_id is not None:
        records = schedule_service.slots_for_person(teacher_id, as_teacher=True)
    elif student_id is not None:
        records = schedule_service.slots_for_person(student_id, as_teacher=False)
    else:
        records = firestore_tuition_slots.list_all()

    return [TuitionSlotOut(**s)
            for s in schedule_service.hydrate_many(schedule_service.sort_slots(records))]


@router.put("/slots/{slot_id}", response_model=TuitionSlotOut)
def update_slot(
    slot_id: int,
    payload: TuitionSlotUpdate,
    allow_conflicts: bool = Query(False),
    current_user: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Move or edit a recurring class time.

    Changes every occurrence from now on. To move a single class instead - "not this Tuesday,
    let us do Wednesday" - reschedule the session, not the slot. Untouched future classes are
    rebuilt at the new time; ones already taught or interacted with are left alone.
    """
    slot = schedule_service.require_slot(slot_id)
    updated = schedule_service.update_slot(slot, payload, current_user, allow_conflicts)
    hydrated = schedule_service.hydrate_many([updated])[0]
    extras = {k: updated[k] for k in ("conflicts", "sessions_generated", "sessions_removed")
              if k in updated}
    return TuitionSlotOut(**{**hydrated, **extras})


@router.delete("/slots/{slot_id}")
def delete_slot(slot_id: int, _: UserOut = Depends(require_admin)):
    """[Admin Only] Remove a recurring class time and its untouched future classes."""
    schedule_service.require_slot(slot_id)
    return {"slot_id": slot_id, **schedule_service.delete_slot(slot_id)}


@router.post("/slots/check-availability", response_model=SlotAvailabilityResult)
def check_slot_availability(payload: SlotAvailabilityQuery, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Ask whether a time is free before committing to it.

    Lets a scheduling UI grey out the impossible times rather than letting an admin fill in a
    form and rejecting it at the end.
    """
    conflicts = schedule_service.free_slot_check(
        payload.enrollment_id, payload.day_of_week, payload.start_time,
        payload.duration_minutes, payload.effective_from, payload.effective_to,
        payload.exclude_slot_id,
    )
    return SlotAvailabilityResult(available=not conflicts, conflicts=conflicts)


@router.get("/conflicts")
def list_conflicts(_: UserOut = Depends(require_admin)):
    """
    [Admin Only] Every clash currently sitting in the timetable.

    Where an overridden booking, or a teacher reassignment that moved a slot into an occupied
    evening, ends up. Nothing here blocks anything - it is the list of things to go and fix.
    """
    findings = schedule_service.all_conflicts()
    return {"conflict_count": len(findings), "conflicts": findings}


@router.get("/schedule/status")
def schedule_status(_: UserOut = Depends(require_admin)):
    """[Admin Only] How far ahead classes have been generated, and from how many slots."""
    return schedule_service.horizon_summary()


@router.post("/schedule/generate", status_code=status.HTTP_202_ACCEPTED)
def generate_sessions(
    horizon_days: int | None = Query(None, ge=1, le=365),
    current_user: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Materialize classes from the recurring slots.

    Runs on its own with the reminder sweep; this is the button for when an admin wants the
    calendar filled now. Idempotent - a class that already exists is left exactly as it is,
    including one somebody has already started or cancelled.
    """
    created = schedule_service.generate_sessions(actor=current_user, horizon_days=horizon_days)
    return {"sessions_generated": created, **schedule_service.horizon_summary()}


# =======================================================================================
# Classes
# =======================================================================================

@router.get("/sessions", response_model=List[TuitionSessionOut])
def list_sessions(
    student_id: int | None = Query(None),
    teacher_id: int | None = Query(None),
    enrollment_id: int | None = Query(None),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    current_user: UserOut = Depends(require_admin),
):
    """[Admin Only] Classes across the programme, with their timing and attendance."""
    if teacher_id is not None:
        records = session_service.list_for_person(teacher_id, as_teacher=True)
    elif student_id is not None:
        records = session_service.list_for_person(student_id, as_teacher=False)
    else:
        records = firestore_tuition_sessions.list_all()

    records = session_service.filter_sessions(
        records, from_date, to_date, status_filter, enrollment_id=enrollment_id
    )
    return [TuitionSessionOut(**s) for s in session_service.present_many(records, current_user)]


@router.post("/sessions", response_model=TuitionSessionOut, status_code=status.HTTP_201_CREATED)
def create_ad_hoc_session(
    payload: TuitionSessionCreate,
    allow_conflicts: bool = Query(False),
    current_user: UserOut = Depends(require_admin),
):
    """[Admin Only] Book a one-off extra class outside the weekly pattern."""
    record = session_service.create_ad_hoc(payload, current_user, allow_conflicts)
    return TuitionSessionOut(**session_service.present(record, current_user))


@router.get("/sessions/{session_id}", response_model=TuitionSessionOut)
def get_session(session_id: str, current_user: UserOut = Depends(require_admin)):
    record = session_service.require_session(session_id)
    return TuitionSessionOut(**session_service.present(record, current_user))


@router.post("/sessions/{session_id}/reschedule", response_model=TuitionSessionOut)
def reschedule_session(
    session_id: str,
    payload: TuitionSessionReschedule,
    allow_conflicts: bool = Query(False),
    current_user: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Move one class without touching the recurring slot behind it.

    Re-checked against both diaries at the new time, and the lateness figures are reset -
    a teacher's lateness on the old date says nothing about the new one.
    """
    record = session_service.require_session(session_id)
    updated = session_service.reschedule_session(
        record, current_user, payload.scheduled_start_at, payload.duration_minutes,
        allow_conflicts,
    )
    return TuitionSessionOut(**session_service.present(updated, current_user))


@router.post("/sessions/{session_id}/cancel", response_model=TuitionSessionOut)
def cancel_session(session_id: str, payload: TuitionSessionCancel,
                   current_user: UserOut = Depends(require_admin)):
    """[Admin Only] Call a class off. Kept as a non-billable record of who cancelled and why."""
    record = session_service.require_session(session_id)
    updated = session_service.cancel_session(record, current_user, payload.reason)
    return TuitionSessionOut(**session_service.present(updated, current_user))


@router.put("/sessions/{session_id}/meeting-link", response_model=TuitionSessionOut)
def set_session_meeting_link(session_id: str, payload: MeetingLinkUpdate,
                             current_user: UserOut = Depends(require_admin)):
    """
    [Admin Only] Point a class at a different meeting provider.

    The brief asks for links other than Google Meet to be usable; this is where a Zoom or
    Teams link goes. A hand-entered link is never overwritten by Meet generation afterwards.
    """
    record = session_service.require_session(session_id)
    updated = session_service.set_meeting_link(record, current_user, payload.meeting_link)
    return TuitionSessionOut(**session_service.present(updated, current_user))


@router.post("/sessions/{session_id}/billable", response_model=TuitionSessionOut)
def set_session_billable(
    session_id: str,
    billable: bool = Query(..., description="Override whether this class counts on the invoice"),
    current_user: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Override whether one class is billed.

    How a goodwill free class is recorded, or a class that happened outside the system is
    brought onto the bill. The override always beats the status the class ended in.
    """
    record = session_service.require_session(session_id)
    firestore_tuition_sessions.add_document(str(session_id), {
        "is_billable": billable, "updated_at": datetime.utcnow().isoformat(),
    })
    return TuitionSessionOut(**session_service.present(
        {**record, "is_billable": billable}, current_user
    ))


# =======================================================================================
# Reports
# =======================================================================================

@router.get("/reports/overview", response_model=ProgrammeReport)
def programme_overview(
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] How many classes every student and every teacher had over a period.

    The report the brief asks for, and the input to the fee run: `conducted` is what an
    invoice is priced from and what a teacher is paid for. Defaults to the last 30 days.

    Note that `attendance_percentage` is measured against classes *conducted*, not scheduled,
    so a student whose teacher missed two classes still reads 100%.
    """
    return report_service.programme_report(from_date, to_date)


@router.get("/reports/students/{student_id}", response_model=StudentAttendanceReport)
def student_attendance_report(
    student_id: int,
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    _: UserOut = Depends(require_admin),
):
    """[Admin Only] One student's attendance, broken down by subject."""
    return report_service.student_report(student_id, from_date, to_date)


@router.get("/reports/teachers/{teacher_id}", response_model=TeacherAttendanceReport)
def teacher_attendance_report(
    teacher_id: int,
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    _: UserOut = Depends(require_admin),
):
    """[Admin Only] One teacher's classes, broken down by student."""
    return report_service.teacher_report(teacher_id, from_date, to_date)


# =======================================================================================
# Fees - admin only, and on no other router
# =======================================================================================

@router.post("/fee-plans", response_model=FeePlanOut, status_code=status.HTTP_201_CREATED)
def create_fee_plan(payload: FeePlanCreate, current_user: UserOut = Depends(require_admin)):
    """
    [Admin Only] What a class costs.

    Plans resolve most-specific-first: an enrollment's own plan, then a plan for that
    subject, then a plan with no subject at all as the programme default. That ordering lets
    a programme set one rate and override it for the one student who negotiated a different
    one, without a plan per enrollment.
    """
    return FeePlanOut(**fee_service.create_plan(payload, current_user))


@router.get("/fee-plans", response_model=List[FeePlanOut])
def list_fee_plans(_: UserOut = Depends(require_admin)):
    from app.core.firebase import firestore_tuition_fee_plans
    return [FeePlanOut(**p) for p in firestore_tuition_fee_plans.list_all()]


@router.put("/fee-plans/{plan_id}", response_model=FeePlanOut)
def update_fee_plan(plan_id: int, payload: FeePlanUpdate, _: UserOut = Depends(require_admin)):
    plan = fee_service.require_plan(plan_id)
    return FeePlanOut(**fee_service.update_plan(plan, payload))


@router.delete("/fee-plans/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_fee_plan(plan_id: int, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Remove a fee plan.

    Invoices already generated keep the figures they were built with, because the price is
    copied onto the invoice line rather than looked up when the bill is read. Deleting a plan
    changes what future classes cost, never what a student has already been billed.
    """
    from app.core.firebase import firestore_tuition_fee_plans
    fee_service.require_plan(plan_id)
    firestore_tuition_fee_plans.delete_document(str(plan_id))


@router.post("/invoices/generate", response_model=InvoiceOut,
             status_code=status.HTTP_201_CREATED)
def generate_invoice(payload: InvoiceGenerate, current_user: UserOut = Depends(require_admin)):
    """
    [Admin Only] Build a student's bill for a period from their conducted classes.

    Derived, not typed in: the session counts price the lines, and both the count and the
    rate are stored on each line so the bill can always be read back as "eight physics
    classes at 120". Re-running on a draft corrects it; an invoice already issued is refused,
    because a bill that silently restates itself after it was sent is not a bill.
    """
    return InvoiceOut(**fee_service.generate_invoice(
        payload.student_id, payload.period_start, payload.period_end, current_user,
        payload.discount_amount, payload.tax_amount, payload.due_date, payload.notes,
    ))


@router.post("/invoices/generate-batch", status_code=status.HTTP_201_CREATED)
def generate_invoice_batch(payload: InvoiceBatchGenerate,
                           current_user: UserOut = Depends(require_admin)):
    """
    [Admin Only] Bill everyone with classes in a period.

    Already-issued invoices are skipped and listed rather than failing the run, so the batch
    can be re-run after fixing one student's data without working out where it got to.
    """
    return fee_service.generate_batch(payload.period_start, payload.period_end, current_user)


@router.get("/invoices", response_model=List[InvoiceOut])
def list_invoices(
    student_id: int | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    _: UserOut = Depends(require_admin),
):
    return [InvoiceOut(**i) for i in
            fee_service.list_invoices(student_id, status_filter, from_date, to_date)]


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
def get_invoice(invoice_id: str, _: UserOut = Depends(require_admin)):
    return InvoiceOut(**fee_service.require_invoice(invoice_id))


@router.post("/invoices/{invoice_id}/issue", response_model=InvoiceOut)
def issue_invoice(invoice_id: str, current_user: UserOut = Depends(require_admin)):
    """[Admin Only] Freeze a draft and mark it sent. The figures stop moving after this."""
    invoice = fee_service.require_invoice(invoice_id)
    return InvoiceOut(**fee_service.issue_invoice(invoice, current_user))


@router.post("/invoices/{invoice_id}/payments", response_model=InvoiceOut)
def record_payment(invoice_id: str, payload: PaymentRecord,
                   current_user: UserOut = Depends(require_admin)):
    """
    [Admin Only] Record money received.

    Payments are appended, so an invoice settled in three instalments shows three. The status
    is re-derived from the running total on every write and cannot drift out of step with the
    payments beneath it.
    """
    invoice = fee_service.require_invoice(invoice_id)
    return InvoiceOut(**fee_service.record_payment(
        invoice, current_user, payload.amount, payload.method, payload.reference, payload.paid_at
    ))


@router.post("/invoices/{invoice_id}/cancel", response_model=InvoiceOut)
def cancel_invoice(invoice_id: str, reason: str | None = Query(None),
                   _: UserOut = Depends(require_admin)):
    invoice = fee_service.require_invoice(invoice_id)
    return InvoiceOut(**fee_service.cancel_invoice(invoice, reason))


@router.get("/fees/summary")
def fee_summary(
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    _: UserOut = Depends(require_admin),
):
    """[Admin Only] Billed against collected over a period, and what is outstanding."""
    return fee_service.revenue_summary(from_date, to_date)


# =======================================================================================
# Billing: the full picture, and getting it out of here
#
# `/invoices` above is the raw collection. These are the screens an administrator actually
# works from - a filtered ledger with its own totals, one student's account, one invoice
# down to the individual classes behind it - plus CSV exports of each.
#
# The exports carry the workings, not just the totals. A CSV of amounts is a report you have
# to trust; one that carries the class count beside the amount, with a second export listing
# those classes by date and attendance, is a report you can check. That is the difference
# between a billing dispute and a lookup.
# =======================================================================================

def _csv_response(content: str, download_name: str) -> Response:
    """
    A CSV download.

    The UTF-8 BOM is deliberate: without it Excel on Windows reads the file as the system
    codepage and mangles every non-ASCII name in it, which for a student list is most of the
    point of the export. Sheets and Numbers ignore it.
    """
    return Response(
        content="﻿" + content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
    )


@router.get("/billing")
def billing_overview(
    student_id: int | None = Query(None),
    status_filter: str | None = Query(None, alias="status",
                                      description="DRAFT | ISSUED | PARTIALLY_PAID | PAID | CANCELLED"),
    from_date: date | None = Query(None, description="Invoices whose period starts on or after"),
    to_date: date | None = Query(None),
    unpaid_only: bool = Query(False, description="Only invoices with something still owed"),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] The billing screen: every invoice matching the filters, with its own totals.

    `summary` is computed from exactly the rows in `invoices`, not from a separate query, so
    the figures at the top can never disagree with the rows underneath - which is the fastest
    way for a billing summary to lose an administrator's trust.

    Each invoice carries its `line_items`, and each line carries the class counts it was
    priced from, so "why is this INR 4,000?" is answerable without opening anything else.
    `overdue_amount` counts only issued invoices past their due date: a draft is never
    overdue, because nobody has been asked to pay it yet.
    """
    invoices = fee_service.list_invoices(student_id, status_filter, from_date, to_date)
    if unpaid_only:
        invoices = [
            i for i in invoices
            if i.get("status") != "CANCELLED"
            and float(i.get("total_amount") or 0) > float(i.get("amount_paid") or 0)
        ]

    currency = program_settings(Program.TUITION.value)["currency"]
    return {
        "filters": {
            "student_id": student_id, "status": status_filter,
            "from_date": from_date, "to_date": to_date, "unpaid_only": unpaid_only,
        },
        "summary": export_service.billing_summary(invoices, currency),
        "invoices": invoices,
    }


@router.get("/billing/students/{student_id}")
def student_billing_account(student_id: int, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] One student's complete billing history.

    The screen to open when a parent calls. Newest period first, with the contact details
    beside the figures, because the question is almost always "what do they owe right now,
    and for what?" and answering it should not need a second lookup.
    """
    invoices = fee_service.list_invoices(student_id)
    currency = program_settings(Program.TUITION.value)["currency"]
    account = export_service.student_billing_history(student_id, invoices)
    account["summary"] = export_service.billing_summary(invoices, currency)
    return account


@router.get("/invoices/{invoice_id}/detail")
def invoice_detail(invoice_id: str, current_user: UserOut = Depends(require_admin)):
    """
    [Admin Only] One invoice down to the individual classes behind every line.

    The full audit trail: the bill, its lines with the counts each was priced from, and then
    the actual sessions in that period - date, status, attendance, who was late and by how
    much, and whether each one was billable.

    Those sessions are read back from the session records rather than stored on the invoice.
    The invoice keeps the *counts* it was priced from; copying every session onto every
    invoice would double the data and give the truth two places to live. `is_billable` on each
    row explains why a class that happened may still not appear in the total.
    """
    invoice = fee_service.require_invoice(invoice_id)
    sessions = export_service.sessions_for_invoice(invoice)

    by_enrollment: dict = {}
    for session in sessions:
        by_enrollment.setdefault(str(session.get("enrollment_id")), []).append(session)

    lines = []
    for line in invoice.get("line_items") or []:
        rows = by_enrollment.get(str(line.get("enrollment_id")), [])
        lines.append({
            **line,
            "sessions": session_service.present_many(rows, current_user),
        })

    total = float(invoice.get("total_amount") or 0)
    paid = float(invoice.get("amount_paid") or 0)
    return {
        **invoice,
        "line_items": lines,
        "outstanding": round(total - paid, 2),
        "session_count": len(sessions),
    }


@router.get("/billing/export")
def export_billing(
    view: str = Query(
        "summary",
        description="summary = one row per invoice; lines = one row per subject, with the "
                    "class counts each amount was priced from; payments = one row per "
                    "payment received; sessions = every class in the period, the evidence "
                    "behind the counts.",
    ),
    student_id: int | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    current_user: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Download the billing data as CSV, using the same filters as `/billing`.

    Four views, because four different people ask for this file: `summary` reconciles against
    the bank, `lines` is what an accountant wants, `payments` is the receipts side, and
    `sessions` is what settles an argument about which classes were charged for.

    UTF-8 with a BOM so Excel on Windows renders names correctly rather than mangling them.
    """
    invoices = fee_service.list_invoices(student_id, status_filter, from_date, to_date)
    period = (from_date, to_date)
    choice = (view or "summary").strip().lower()

    if choice == "lines":
        body = export_service.invoice_lines_csv(invoices)
    elif choice == "payments":
        body = export_service.payments_csv(invoices)
    elif choice == "sessions":
        rows = []
        for invoice in invoices:
            rows.extend(export_service.sessions_for_invoice(invoice))
        body = export_service.sessions_csv(rows, current_user)
    elif choice == "summary":
        body = export_service.invoices_csv(invoices)
    else:
        raise HTTPException(
            status_code=400,
            detail="view must be one of: summary, lines, payments, sessions.",
        )

    return _csv_response(body, export_service.filename(f"tuition-billing-{choice}", *period))


@router.get("/invoices/{invoice_id}/export")
def export_invoice(
    invoice_id: str,
    view: str = Query("lines", description="lines = the invoice's subjects; sessions = the "
                                           "classes behind them"),
    current_user: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Download one invoice as CSV - the subjects billed, or the classes behind them.

    `sessions` is the attachment to send a parent who queries a bill: every class in the
    period with its date, status and attendance, so the count on the invoice can be checked
    line by line rather than taken on trust.
    """
    invoice = fee_service.require_invoice(invoice_id)
    choice = (view or "lines").strip().lower()

    if choice == "sessions":
        body = export_service.sessions_csv(
            export_service.sessions_for_invoice(invoice), current_user
        )
    elif choice == "lines":
        body = export_service.invoice_lines_csv([invoice])
    else:
        raise HTTPException(status_code=400, detail="view must be one of: lines, sessions.")

    return _csv_response(body, export_service.filename(f"invoice-{invoice_id}-{choice}"))


@router.get("/reports/export")
def export_reports(
    view: str = Query("students", description="students | teachers | sessions"),
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    current_user: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Download the class and attendance counts as CSV.

    The same figures as `/reports/overview` - and the basis on which fees are planned, so this
    is the file to check before running a month's billing rather than after.
    """
    choice = (view or "students").strip().lower()
    report = report_service.programme_report(from_date, to_date)
    period = (report["from_date"], report["to_date"])

    if choice == "students":
        body = export_service.report_rows_csv(
            report["students"], "Student", "student_name", "student_id",
            extra=[("Admission Number", "admission_number"), ("Subjects Taken", "subjects_taken")],
        )
    elif choice == "teachers":
        body = export_service.report_rows_csv(
            report["teachers"], "Teacher", "teacher_name", "teacher_id",
            extra=[("Employee ID", "employee_id"), ("Students Taught", "students_taught")],
        )
    elif choice == "sessions":
        rows = session_service.filter_sessions(
            firestore_tuition_sessions.list_all(), report["from_date"], report["to_date"]
        )
        body = export_service.sessions_csv(rows, current_user)
    else:
        raise HTTPException(
            status_code=400, detail="view must be one of: students, teachers, sessions."
        )

    return _csv_response(body, export_service.filename(f"tuition-report-{choice}", *period))


# =======================================================================================
# Reminders and the background sweep
# =======================================================================================

@router.get("/reminders/status")
def reminder_status(_: UserOut = Depends(require_admin)):
    """[Admin Only] Whether tuition reminders are running, at what lead times, and in which zone."""
    return reminder_service.get_scheduler().status()


@router.get("/reminders/preview")
def preview_reminders(_: UserOut = Depends(require_admin)):
    """
    [Admin Only] What would go out right now, without sending anything.

    A dry run claims nothing, so an admin who has just changed the lead time can check the
    effect as often as they like before it reaches anybody's inbox.
    """
    return reminder_service.sweep(send=False)


@router.post("/reminders/run", status_code=status.HTTP_202_ACCEPTED)
def run_reminders_now(_: UserOut = Depends(require_admin)):
    """[Admin Only] Run the tuition reminder sweep immediately."""
    return reminder_service.sweep()


@router.post("/maintenance/run", status_code=status.HTTP_202_ACCEPTED)
def run_maintenance_now(_: UserOut = Depends(require_admin)):
    """
    [Admin Only] Extend the generated-class horizon and settle classes nobody closed.

    Runs on its own with the reminder sweep. Exposed because the two housekeeping jobs are
    what make counts and invoices correct, and an admin about to run the month's billing
    reasonably wants them done first.
    """
    return reminder_service.maintenance_pass()
