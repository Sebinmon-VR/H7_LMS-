"""
Homework, staff leave, and the reports that say whether the teaching rhythm is happening.

Four routers, grouped here because they are the academic-management half of the brief:

  * `/homework`        - teachers set it, students hand it in, teachers mark it.
  * `/leave`           - staff apply, admins decide.
  * `/admin/academics` - the cadence and counts reports an administrator acts on.
  * `/admin/reports`   - the weekly and monthly digests emailed to parents.

The cadence report is the one worth reading the docstring for. It reports; it never blocks.
A system that refused to let a teacher take a class because last week's test was missing
would be switched off within a fortnight.
"""

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.v1.dependencies import (
    require_admin, require_any_authenticated, require_student, require_teacher,
)
from app.core.enums import LeaveStatus, LeaveType, UserRole
from app.core.firebase import firestore_homework_submissions, firestore_users, require_document
from app.schemas.user import UserOut
from app.schemas.workflow import (
    HomeworkCreate, HomeworkGrade, HomeworkOut, HomeworkSubmissionOut, HomeworkSubmit,
    HomeworkUpdate, LeaveApply, LeaveBalanceOut, LeaveDecision, LeaveRequestOut,
)
from app.services import cadence as cadence_service
from app.services import homework as homework_service
from app.services import leave as leave_service
from app.services import parent_reports
from app.services import permissions

homework_router = APIRouter(prefix="/homework", tags=["Homework"])
leave_router = APIRouter(prefix="/leave", tags=["Staff Leave"])
academics_router = APIRouter(prefix="/admin/academics", tags=["Academic Oversight (Admin)"])
reports_router = APIRouter(prefix="/admin/reports", tags=["Parent Reports (Admin)"])


# ---------------------------------------------------------------------------------------
# Homework
# ---------------------------------------------------------------------------------------

@homework_router.post("", response_model=HomeworkOut, status_code=status.HTTP_201_CREATED)
def set_homework(payload: HomeworkCreate, teacher: UserOut = Depends(require_teacher)):
    """
    Set homework for a class.

    You may only set it for a subject you are mapped to teach that class, or for any subject
    in a class you lead. `is_mandatory` decides whether it counts towards the daily-homework
    obligation the admin's cadence report tracks.
    """
    assignment = homework_service.create_assignment(payload, teacher)
    return HomeworkOut(**homework_service.present(assignment, teacher))


@homework_router.get("", response_model=List[HomeworkOut])
def list_homework(
    class_id: Optional[int] = Query(None),
    from_date: Optional[date] = Query(None, description="Filtered on the due date."),
    to_date: Optional[date] = Query(None),
    user: UserOut = Depends(require_any_authenticated),
):
    """
    Homework, newest due date first.

    A student sees their own class's work with their own status on each item; a teacher sees
    what they set, with submission counts. Both come from one function, so the counts and the
    statuses cannot disagree about what "submitted" means.
    """
    from app.core.firebase import firestore_homework

    if user.role == UserRole.STUDENT:
        # A student sees their own class's work and nothing else; `class_id` is ignored
        # rather than honoured, so it cannot be used to read another class's assignments.
        student_class = homework_service._class_of(user.id)
        if student_class is None:
            return []
        assignments = homework_service.for_class(student_class, from_date, to_date)

    elif user.role == UserRole.ADMIN:
        assignments = (
            homework_service.for_class(class_id, from_date, to_date)
            if class_id is not None
            else homework_service._filter_by_date(
                firestore_homework.list_all(), from_date, to_date
            )
        )

    elif class_id is not None:
        # A teacher asking about one class gets what they set there, plus everything set by
        # anyone if they lead that class - a class teacher is answerable for the whole class,
        # not only their own periods.
        leads = permissions.is_class_teacher_of(user, class_id)
        assignments = [
            a for a in homework_service.for_class(class_id, from_date, to_date)
            if leads or int(a.get("teacher_id", -1)) == int(user.id)
        ]

    else:
        assignments = homework_service.for_teacher(user.id, from_date, to_date)

    return [HomeworkOut(**homework_service.present(a, user)) for a in assignments]


@homework_router.get("/{assignment_id}", response_model=HomeworkOut)
def get_homework(assignment_id: int, user: UserOut = Depends(require_any_authenticated)):
    """One assignment."""
    assignment = homework_service.require_assignment(assignment_id)
    if user.role == UserRole.STUDENT:
        homework_service.assert_is_for(assignment, user.id)
    return HomeworkOut(**homework_service.present(assignment, user))


@homework_router.put("/{assignment_id}", response_model=HomeworkOut)
def update_homework(
    assignment_id: int, payload: HomeworkUpdate, teacher: UserOut = Depends(require_teacher)
):
    """
    Edit an assignment. Partial: omitted fields are left unchanged.

    The deadline may be extended freely. Moving it *earlier* past work already handed in is
    refused - those submissions were on time when they were made, and a retroactive deadline
    would mark them late.
    """
    assignment = homework_service.require_assignment(assignment_id)
    homework_service._assert_may_set(
        assignment["class_id"], assignment["subject_id"], teacher
    )
    updated = homework_service.update_assignment(assignment, payload, teacher)
    return HomeworkOut(**homework_service.present(updated, teacher))


@homework_router.delete("/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_homework(assignment_id: int, teacher: UserOut = Depends(require_teacher)):
    """Delete an assignment and every submission against it."""
    assignment = homework_service.require_assignment(assignment_id)
    homework_service._assert_may_set(
        assignment["class_id"], assignment["subject_id"], teacher
    )
    homework_service.delete_assignment(assignment)


@homework_router.post("/{assignment_id}/submit", response_model=HomeworkSubmissionOut)
def submit_homework(
    assignment_id: int, payload: HomeworkSubmit, student: UserOut = Depends(require_student)
):
    """
    [Student] Hand in work.

    Submitting twice updates your work rather than filing a second copy. Late submissions are
    accepted or refused per assignment; when accepted they are flagged rather than hidden -
    a deadline that silently accepts everything has no meaning.

    409 once the work has been marked. Ask your teacher to reopen it.
    """
    assignment = homework_service.require_assignment(assignment_id)
    submission = homework_service.submit(assignment, student.id, payload)
    return HomeworkSubmissionOut(
        **homework_service.submission_view(submission, assignment, student.id)
    )


@homework_router.get("/{assignment_id}/submissions", response_model=List[HomeworkSubmissionOut])
def list_submissions(assignment_id: int, teacher: UserOut = Depends(require_teacher)):
    """
    [Teacher] Every student's position on one assignment - the marking list.

    Includes students who have filed nothing, as ASSIGNED before the due date and MISSED
    after it. Those states are derived from the clock and never stored, so the list is right
    the morning after rather than whenever somebody last ran a sweep.
    """
    assignment = homework_service.require_assignment(assignment_id)
    homework_service._assert_may_set(
        assignment["class_id"], assignment["subject_id"], teacher
    )

    filed = {
        int(s["student_id"]): s for s in homework_service.submissions_for(assignment_id)
    }
    rows = []
    for student_id in homework_service.students_in_class(assignment["class_id"]):
        rows.append(homework_service.submission_view(
            filed.get(student_id), assignment, student_id
        ))

    rows.sort(key=lambda r: (r["status"] != "SUBMITTED", str(r.get("student_name") or "")))
    return [HomeworkSubmissionOut(**r) for r in rows]


@homework_router.post("/submissions/{submission_id}/grade", response_model=HomeworkSubmissionOut)
def grade_submission(
    submission_id: str, payload: HomeworkGrade, teacher: UserOut = Depends(require_teacher)
):
    """[Teacher] Record a mark and feedback. Refused if the mark exceeds `max_marks`."""
    submission = require_document(
        firestore_homework_submissions, submission_id, "Homework submission"
    )
    graded = homework_service.grade(submission, payload, teacher)
    assignment = homework_service.require_assignment(submission["assignment_id"])
    return HomeworkSubmissionOut(
        **homework_service.submission_view(graded, assignment)
    )


@homework_router.post("/submissions/{submission_id}/reopen", response_model=HomeworkSubmissionOut)
def reopen_submission(submission_id: str, teacher: UserOut = Depends(require_teacher)):
    """
    [Teacher] Clear a mark so the student may resubmit.

    The counterpart to refusing a resubmission after marking: the teacher decides, rather
    than the student working around it.
    """
    submission = require_document(
        firestore_homework_submissions, submission_id, "Homework submission"
    )
    reopened = homework_service.reopen(submission, teacher)
    assignment = homework_service.require_assignment(submission["assignment_id"])
    return HomeworkSubmissionOut(
        **homework_service.submission_view(reopened, assignment)
    )


# ---------------------------------------------------------------------------------------
# Staff leave
# ---------------------------------------------------------------------------------------

@leave_router.post("", response_model=LeaveRequestOut, status_code=status.HTTP_201_CREATED)
def apply_for_leave(payload: LeaveApply, staff: UserOut = Depends(require_teacher)):
    """
    Apply to be away.

    The periods you are timetabled to take across the dates are snapshotted onto the request,
    so whoever approves it can see what they are agreeing to cover.

    Refused if it overlaps one of your own live requests - nearly always a double submission
    rather than an intention to be away twice at once.
    """
    request = leave_service.apply_for_leave(payload, staff)
    return LeaveRequestOut(**leave_service.present(request))


@leave_router.get("/me", response_model=List[LeaveRequestOut])
def my_leave(
    status_filter: Optional[LeaveStatus] = Query(None, alias="status"),
    staff: UserOut = Depends(require_teacher),
):
    """My own leave requests, pending first."""
    requests = leave_service.list_requests(
        teacher_id=staff.id, status=status_filter.value if status_filter else None
    )
    return [LeaveRequestOut(**leave_service.present(r)) for r in requests]


@leave_router.get("/me/balance", response_model=LeaveBalanceOut)
def my_leave_balance(
    academic_year_id: Optional[int] = Query(None),
    staff: UserOut = Depends(require_teacher),
):
    """
    My leave, totalled by type.

    Taken and pending are separate figures: a request awaiting a decision is neither granted
    nor free, and one combined number is how two people get approved for the same week.
    """
    return LeaveBalanceOut(**leave_service.balance_for(staff.id, academic_year_id))


@leave_router.get("", response_model=List[LeaveRequestOut])
def list_leave(
    teacher_id: Optional[int] = Query(None),
    status_filter: Optional[LeaveStatus] = Query(None, alias="status"),
    leave_type: Optional[LeaveType] = Query(None),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    _: UserOut = Depends(require_admin),
):
    """[Admin Only] Every leave request, pending first - the approval queue."""
    requests = leave_service.list_requests(
        teacher_id=teacher_id,
        status=status_filter.value if status_filter else None,
        leave_type=leave_type.value if leave_type else None,
        from_date=from_date, to_date=to_date,
    )
    return [LeaveRequestOut(**leave_service.present(r)) for r in requests]


@leave_router.get("/on/{day}", response_model=List[LeaveRequestOut])
def who_is_away(day: date, _: UserOut = Depends(require_teacher)):
    """
    Who is approved to be away on a date - the daily cover sheet.

    Only approved leave counts. A pending request is not yet an absence, and treating it as
    one would have the office arranging cover for a day off nobody has granted.
    """
    return [LeaveRequestOut(**leave_service.present(r)) for r in leave_service.on_leave_on(day)]


@leave_router.post("/{request_id}/decide", response_model=LeaveRequestOut)
def decide_leave(
    request_id: int, payload: LeaveDecision, admin: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Approve or reject a leave request.

    A rejection needs a note. Naming a `substitute_teacher_id` is optional - many schools
    decide cover separately, and requiring it would block approvals on a decision nobody has
    made yet.
    """
    request = leave_service.require_request(request_id)
    decided = leave_service.decide(
        request, payload.approve, admin, payload.note, payload.substitute_teacher_id
    )
    return LeaveRequestOut(**leave_service.present(decided))


@leave_router.post("/{request_id}/withdraw", response_model=LeaveRequestOut)
def withdraw_leave(request_id: int, staff: UserOut = Depends(require_teacher)):
    """
    Take your own request back, before or after approval.

    Plans change, and a teacher who no longer needs the day should be able to return it
    rather than have it counted against them. An admin doing this to somebody else's approved
    leave is recorded as CANCELLED rather than WITHDRAWN.
    """
    request = leave_service.require_request(request_id)
    return LeaveRequestOut(**leave_service.present(leave_service.withdraw(request, staff)))


@leave_router.get("/balance/{teacher_id}", response_model=LeaveBalanceOut)
def teacher_leave_balance(
    teacher_id: int,
    academic_year_id: Optional[int] = Query(None),
    _: UserOut = Depends(require_admin),
):
    """[Admin Only] One teacher's leave totals."""
    require_document(firestore_users, teacher_id, "Teacher")
    return LeaveBalanceOut(**leave_service.balance_for(teacher_id, academic_year_id))


# ---------------------------------------------------------------------------------------
# Cadence oversight
# ---------------------------------------------------------------------------------------

@academics_router.get("/cadence")
def cadence_report(
    class_id: Optional[int] = Query(None),
    teacher_id: Optional[int] = Query(None),
    overdue_only: bool = Query(True, description="Only pairs that have fallen behind."),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Whether the weekly exam and daily homework obligations are being met.

    One row per (class, subject) pair, built from the teacher mappings - so it reports on
    pairs that *should* have had an exam, not only on the teachers already complying.

    The seven days are a rolling gap between consecutive exams, not a calendar week. A
    calendar week lets a teacher set exams on a Friday and the next Monday and miss eleven
    days in between while appearing compliant in both.

    Worst first: subjects never examined at the top, then longest overdue. This reports; it
    never blocks anybody from teaching.
    """
    return cadence_service.compliance_report(class_id, teacher_id, overdue_only)


@academics_router.get("/exam-counts")
def exam_counts(
    class_id: Optional[int] = Query(None),
    from_date: Optional[date] = Query(None, description="Defaults to four weeks back."),
    to_date: Optional[date] = Query(None),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] How many exams each student actually sat, against how many were set.

    The pair is the useful figure: a student who sat 3 of 8 and one who sat 3 of 3 have the
    same count and opposite problems. Sorted worst attendance first - this is a list for
    chasing students, so the ones who need chasing are at the top.
    """
    return cadence_service.student_exam_counts(class_id, from_date, to_date)


@academics_router.get("/homework-counts")
def homework_counts(
    class_id: Optional[int] = Query(None),
    from_date: Optional[date] = Query(None, description="Defaults to one week back."),
    to_date: Optional[date] = Query(None),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Homework submitted, late and missed, per student.

    MISSED is derived from the due date having passed with nothing filed - the same
    derivation the student's own screen uses, so the two always agree.
    """
    return cadence_service.student_homework_counts(class_id, from_date, to_date)


# ---------------------------------------------------------------------------------------
# Parent digests
# ---------------------------------------------------------------------------------------

@reports_router.get("/parent-preview/{student_id}")
def preview_parent_report(
    student_id: int,
    period: str = Query("WEEKLY", description="WEEKLY or MONTHLY"),
    from_date: Optional[date] = Query(None),
    to_date: Optional[date] = Query(None),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] The digest a parent would receive, without sending it.

    A report is a view over attendance, marks, homework and fees rather than anything stored,
    so any past period can be previewed and a wrong figure is fixed by correcting the record
    behind it. The fee section is omitted here - it is decided per recipient by their own
    link's `may_view_fees`.
    """
    require_document(firestore_users, student_id, "Student")
    return parent_reports.build_report(student_id, period.upper(), from_date, to_date)


@reports_router.post("/parent-send/{student_id}")
def send_parent_report(
    student_id: int,
    period: str = Query("WEEKLY"),
    force: bool = Query(
        False,
        description="Send even if already sent for this period, or if nothing happened.",
    ),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Email one child's digest to every parent entitled to it.

    Each recipient gets a report built for their own permissions - the fee section appears
    only for a link granting `may_view_fees`.

    Idempotent per recipient per period: a second call reports "already sent" rather than
    mailing twice. A period in which nothing happened is skipped, because an email that says
    nothing trains people to stop opening the ones that do.
    """
    require_document(firestore_users, student_id, "Student")
    return parent_reports.send_report(student_id, period.upper(), force=force)


@reports_router.post("/parent-sweep", status_code=status.HTTP_202_ACCEPTED)
def sweep_parent_reports(
    period: str = Query("WEEKLY", description="WEEKLY or MONTHLY"),
    class_id: Optional[int] = Query(None, description="Limit to one class."),
    force: bool = Query(False),
    _: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Send the digest for every active student.

    Covers the last **complete** week or month, not the current partial one - a Monday digest
    covering the week that started that morning reports nothing.

    Safe to run more than once: the send log makes a repeat a no-op per recipient.
    """
    return parent_reports.sweep(period.upper(), class_id, force)
