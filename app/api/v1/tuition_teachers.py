"""
Online tuition - teacher module.

A tuition teacher's day is narrower than a school teacher's and the routes reflect it: their
timetable, the class they are about to take, attendance for the one student in it, the work
they set, and their own numbers.

Every route is guarded twice - the role guard, then the programme guard - so a school teacher
with a valid login and the TEACHER role cannot reach any of it. Beyond that, ownership is
checked per record: a teacher sees the students they teach and nobody else's, which in a
one-to-one product is a privacy boundary rather than a convenience.
"""

import logging
from datetime import date
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.v1.dependencies import require_tuition_teacher
from app.schemas.exam import ExamOut
from app.schemas.tuition import (
    MeetingLinkUpdate, TeacherAttendanceReport, TuitionAssessmentCreate,
    TuitionAttendanceMark, TuitionEnrollmentOut, TuitionReportCardCreate,
    TuitionSessionCancel, TuitionSessionCreate, TuitionSessionEnd, TuitionSessionOut,
    TuitionSessionReschedule, TuitionSlotOut,
)
from app.schemas.user import UserOut
from app.services.exams import hydrate_exam, prefetch_exams, require_exam
from app.services.tuition import (
    assessments as assessment_service,
    enrollments as enrollment_service,
    reports as report_service,
    scheduling as schedule_service,
    sessions as session_service,
)

logger = logging.getLogger("tuition_teacher_api")

router = APIRouter(prefix="/tuition/teachers", tags=["Tuition - Teacher Module"])


# =======================================================================================
# My students
# =======================================================================================

@router.get("/my-students", response_model=List[TuitionEnrollmentOut])
def my_students(
    include_inactive: bool = Query(False),
    current_user: UserOut = Depends(require_tuition_teacher),
):
    """
    [Tuition Teacher] The students assigned to me, with their details and what I teach them.

    Returns the student's full profile alongside the syllabus and goals recorded on the
    arrangement - the brief asks for the teacher to have the student's data in front of them,
    not an id to go and look up.
    """
    records = enrollment_service.for_teacher(current_user.id, include_inactive)
    return [TuitionEnrollmentOut(**e) for e in enrollment_service.hydrate_many(records)]


# =======================================================================================
# My timetable
# =======================================================================================

@router.get("/timetable", response_model=List[TuitionSlotOut])
def my_timetable(current_user: UserOut = Depends(require_tuition_teacher)):
    """
    [Tuition Teacher] My recurring weekly class times.

    The pattern, not the calendar. For actual classes on actual dates - including ones moved
    or cancelled - use `/sessions`, which is what a teacher should be looking at on the day.
    """
    records = schedule_service.slots_for_person(current_user.id, as_teacher=True)
    return [TuitionSlotOut(**s)
            for s in schedule_service.hydrate_many(schedule_service.sort_slots(records))]


@router.get("/sessions", response_model=List[TuitionSessionOut])
def my_sessions(
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    student_id: int | None = Query(None),
    current_user: UserOut = Depends(require_tuition_teacher),
):
    """
    [Tuition Teacher] My classes, with times shown in my own timezone.

    Each class carries a `timing` block with the countdown resolved on the server - including
    `may_end_now`, which is the flag that tells a teacher whether they have yet reached the
    point at which they are entitled to stop.
    """
    records = session_service.list_for_person(current_user.id, as_teacher=True)
    records = session_service.filter_sessions(records, from_date, to_date, status_filter)
    if student_id is not None:
        records = [r for r in records if r.get("student_id") == student_id]
    return [TuitionSessionOut(**s) for s in session_service.present_many(records, current_user)]


@router.get("/sessions/upcoming", response_model=List[TuitionSessionOut])
def my_upcoming(
    limit: int = Query(10, ge=1, le=50),
    current_user: UserOut = Depends(require_tuition_teacher),
):
    """[Tuition Teacher] The next few classes I have to take."""
    from app.services.tuition.common import now_utc, to_utc

    moment = now_utc()
    records = [
        s for s in session_service.list_for_person(current_user.id, as_teacher=True)
        if s.get("status") in {"SCHEDULED", "IN_PROGRESS"}
        and (to_utc(s.get("scheduled_end_at")) or moment) >= moment
    ]
    records = session_service.sort_sessions(records)[:limit]
    return [TuitionSessionOut(**s) for s in session_service.present_many(records, current_user)]


@router.get("/sessions/{session_id}", response_model=TuitionSessionOut)
def get_session(session_id: str, current_user: UserOut = Depends(require_tuition_teacher)):
    record = session_service.require_session(session_id)
    session_service.assert_may_view(record, current_user)
    return TuitionSessionOut(**session_service.present(record, current_user))


# =======================================================================================
# Taking a class
# =======================================================================================

@router.post("/sessions/{session_id}/start", response_model=TuitionSessionOut)
def start_class(session_id: str, current_user: UserOut = Depends(require_tuition_teacher)):
    """
    [Tuition Teacher] Join and start the class.

    Records the arrival that the timing rule turns on, and does two separate things:

    - **If you are late, this is what buys the student their extra time.** The class end
      moves out to a full lesson from the moment you actually joined, capped by the
      administrator's limit.
    - **It starts the clock the student is judged against.** Until you press this, the class
      has not begun and the student cannot be late for it - somebody waiting since 17:00 for
      a class you open at 17:12 is on time, and only the minutes after 17:12 count against
      them. (Unless the administrator has switched on `auto_start_class`, in which case the
      class opens on its timetabled slot and lateness runs from the timetable.)

    Joining twice does not reset either: the first arrival is the one that counts, so a page
    refresh cannot quietly take back time the student is owed.
    """
    record = session_service.require_session(session_id)
    updated = session_service.teacher_join(record, current_user)
    return TuitionSessionOut(**session_service.present(updated, current_user))


@router.post("/sessions/{session_id}/end", response_model=TuitionSessionOut)
def end_class(session_id: str, payload: TuitionSessionEnd,
              current_user: UserOut = Depends(require_tuition_teacher)):
    """
    [Tuition Teacher] Close the class and record what was taught.

    You may stop once `timing.may_end_now` is true - the end of the period, extended if you
    were late, not extended if the student was. Ending earlier is allowed, because
    connections drop and students leave, but it is recorded as `ended_early` and appears on
    the administrator's report.

    A class you attended and the student did not is closed as a student no-show rather than
    as a completed lesson, which is the distinction the fee module reads.
    """
    record = session_service.require_session(session_id)
    updated = session_service.end_session(
        record, current_user, payload.topic, payload.notes, payload.recording_url
    )
    return TuitionSessionOut(**session_service.present(updated, current_user))


@router.post("/sessions/{session_id}/attendance", response_model=TuitionSessionOut)
def mark_attendance(session_id: str, payload: TuitionAttendanceMark,
                    current_user: UserOut = Depends(require_tuition_teacher)):
    """
    [Tuition Teacher] Record whether the student attended.

    Your call, and it overrides what the join timestamps imply - a student whose connection
    failed and who phoned in is present, however the log reads. The response carries
    `suggested_attendance`, which is what the timestamps say, offered as a default rather
    than applied.
    """
    record = session_service.require_session(session_id)
    updated = session_service.mark_attendance(
        record, current_user, payload.status, payload.remarks
    )
    return TuitionSessionOut(**session_service.present(updated, current_user))


@router.post("/sessions/{session_id}/meeting-link", response_model=TuitionSessionOut)
def create_or_replace_meeting_link(
    session_id: str,
    payload: MeetingLinkUpdate | None = None,
    current_user: UserOut = Depends(require_tuition_teacher),
):
    """
    [Tuition Teacher] Get the class a link, or replace it with your own.

    With a body, your link is used as-is - Zoom, Teams, a permanent Meet room - and is never
    overwritten afterwards. Without one, a Google Meet is generated on the spot. Meet links
    are made on demand rather than for every class in the calendar, so a month of scheduled
    classes does not become a month of Calendar API calls for classes that may never happen.
    """
    record = session_service.require_session(session_id)
    session_service.assert_is_teacher_of(record, current_user)

    if payload and payload.meeting_link:
        updated = session_service.set_meeting_link(record, current_user, payload.meeting_link)
    else:
        updated = session_service.ensure_meeting_link(record, current_user, force=True)
    return TuitionSessionOut(**session_service.present(updated, current_user))


@router.post("/sessions", response_model=TuitionSessionOut, status_code=status.HTTP_201_CREATED)
def create_extra_class(
    payload: TuitionSessionCreate,
    current_user: UserOut = Depends(require_tuition_teacher),
):
    """
    [Tuition Teacher] Book a one-off extra class - revision, or making up a cancelled one.

    Conflict-checked against both diaries, so an extra class cannot be dropped on top of
    something the student already has with a different teacher. A teacher may only do this
    for a student they are assigned to.
    """
    enrollment = enrollment_service.require_enrollment(payload.enrollment_id)
    if enrollment.get("teacher_id") != current_user.id:
        raise HTTPException(status_code=403, detail="This student is not assigned to you.")
    record = session_service.create_ad_hoc(payload, current_user)
    return TuitionSessionOut(**session_service.present(record, current_user))


@router.post("/sessions/{session_id}/reschedule", response_model=TuitionSessionOut)
def reschedule(session_id: str, payload: TuitionSessionReschedule,
               current_user: UserOut = Depends(require_tuition_teacher)):
    """
    [Tuition Teacher] Move one class. The weekly slot is untouched.

    To change every Tuesday from now on, an administrator edits the slot - a teacher moving
    one class should not silently reshape a student's term.
    """
    record = session_service.require_session(session_id)
    session_service.assert_is_teacher_of(record, current_user)
    updated = session_service.reschedule_session(
        record, current_user, payload.scheduled_start_at, payload.duration_minutes
    )
    return TuitionSessionOut(**session_service.present(updated, current_user))


@router.post("/sessions/{session_id}/cancel", response_model=TuitionSessionOut)
def cancel(session_id: str, payload: TuitionSessionCancel,
           current_user: UserOut = Depends(require_tuition_teacher)):
    """[Tuition Teacher] Call a class off. Recorded as cancelled by you, and not billed."""
    record = session_service.require_session(session_id)
    session_service.assert_is_teacher_of(record, current_user)
    updated = session_service.cancel_session(record, current_user, payload.reason)
    return TuitionSessionOut(**session_service.present(updated, current_user))


# =======================================================================================
# Homework, assignments and exams
# =======================================================================================

@router.post("/assessments", response_model=ExamOut, status_code=status.HTTP_201_CREATED)
def set_work(payload: TuitionAssessmentCreate,
             current_user: UserOut = Depends(require_tuition_teacher)):
    """
    [Tuition Teacher] Set homework, an assignment or an exam for one student.

    The full LMS exam engine, addressed to one student instead of a class: question types,
    answer keys, timed windows, per-student extra time, auto-marking of objective questions,
    late-upload concessions, valuation and published results all behave exactly as they do
    for a school exam.

    `category` (HOMEWORK, ASSIGNMENT, EXAM, TEST, PROJECT) is a label - one-to-one teaching
    sets far more homework than exams, and a student's list that cannot tell them apart is
    unusable. Create as DRAFT and publish when ready; a draft is invisible to the student,
    not merely unlisted.
    """
    record = assessment_service.create_assessment(payload, current_user)
    return ExamOut(**hydrate_exam(record))


@router.get("/assessments", response_model=List[ExamOut])
def list_work(
    category: str | None = Query(None),
    student_id: int | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    current_user: UserOut = Depends(require_tuition_teacher),
):
    """[Tuition Teacher] The work I have set, nearest deadline first."""
    records = assessment_service.for_teacher(current_user.id)
    records = assessment_service.filter_assessments(records, category, status=status_filter)
    if student_id is not None:
        records = [r for r in records if r.get("student_id") == student_id]
    prefetch_exams(records)
    return [ExamOut(**hydrate_exam(r)) for r in records]


@router.get("/assessments/{exam_id}", response_model=ExamOut)
def get_work(exam_id: int, current_user: UserOut = Depends(require_tuition_teacher)):
    exam = require_exam(exam_id)
    assessment_service.assert_is_tuition(exam)
    assessment_service.assert_may_manage(exam, current_user)
    return ExamOut(**hydrate_exam(exam))


# =======================================================================================
# Report cards
# =======================================================================================

@router.post("/report-cards", status_code=status.HTTP_201_CREATED)
def generate_report_card(payload: TuitionReportCardCreate,
                         current_user: UserOut = Depends(require_tuition_teacher)):
    """
    [Tuition Teacher] Consolidate a student's tuition work onto one card.

    Same arithmetic as the school report card - grade bands, subject totals, and the rule
    that a paper never sat is *not* averaged in as zero unless you ask for it. Two
    differences: the attendance figure comes from tuition classes rather than school
    registers, and there is no rank, because a one-to-one student has no cohort to be ranked
    against.

    Generating does not release it. Read it, add a remark, then publish.
    """
    if current_user.id not in {
        e.get("teacher_id")
        for e in enrollment_service.for_student(payload.student_id, include_inactive=True)
    }:
        raise HTTPException(status_code=403, detail="You do not teach this student.")
    return assessment_service.generate_report_card(payload, current_user)


@router.post("/report-cards/{card_id}/publish")
def publish_report_card(card_id: str, current_user: UserOut = Depends(require_tuition_teacher)):
    """[Tuition Teacher] Release a card to the student."""
    from app.core.firebase import firestore_report_cards

    card = firestore_report_cards.get_document(card_id)
    if not card or card.get("program") != "TUITION":
        raise HTTPException(status_code=404, detail="Report card not found")
    if current_user.id not in {
        e.get("teacher_id")
        for e in enrollment_service.for_student(card.get("student_id"), include_inactive=True)
    }:
        raise HTTPException(status_code=403, detail="You do not teach this student.")
    return assessment_service.publish_card({**card, "id": card_id})


# =======================================================================================
# My own numbers
# =======================================================================================

@router.get("/reports/me", response_model=TeacherAttendanceReport)
def my_attendance_report(
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    current_user: UserOut = Depends(require_tuition_teacher),
):
    """
    [Tuition Teacher] My own class and attendance record, broken down by student.

    Mine only - there is no parameter for another teacher's, by design. The brief asks that
    teachers see their own attendance report, and in a product where every class is one
    family's private arrangement, one teacher browsing another's is not a report, it is a
    leak.
    """
    return report_service.teacher_report(current_user.id, from_date, to_date)


@router.get("/reports/students/{student_id}")
def student_report(
    student_id: int,
    from_date: date | None = Query(None),
    to_date: date | None = Query(None),
    current_user: UserOut = Depends(require_tuition_teacher),
):
    """
    [Tuition Teacher] One of my students' attendance, limited to the subjects I teach them.

    A teacher who takes a student for physics has no business seeing how that student is
    doing in maths with somebody else, so the subject rows are filtered to their own
    arrangements rather than the student's whole record.
    """
    mine = {
        e.get("id") for e in enrollment_service.for_teacher(current_user.id, include_inactive=True)
        if e.get("student_id") == student_id
    }
    if not mine:
        raise HTTPException(status_code=403, detail="This student is not assigned to you.")

    report = report_service.student_report(student_id, from_date, to_date)
    report["subjects"] = [
        row for row in report["subjects"] if str(row.get("enrollment_id")) in {str(m) for m in mine}
    ]
    report["totals"] = report_service.summarize([
        s for s in session_service.filter_sessions(
            session_service.list_for_person(current_user.id, as_teacher=True),
            from_date, to_date,
        ) if s.get("student_id") == student_id
    ])
    return report
