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
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.v1.dependencies import require_tuition_student
from app.schemas.exam import ExamOut
from app.schemas.tuition import (
    StudentAttendanceReport, TuitionEnrollmentOut, TuitionSessionOut, TuitionSlotOut,
)
from app.schemas.user import UserOut
from app.services.exams import hydrate_exam_for_student, prefetch_exams
from app.services.tuition import (
    assessments as assessment_service,
    enrollments as enrollment_service,
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
