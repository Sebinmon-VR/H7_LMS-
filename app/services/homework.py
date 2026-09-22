"""
Homework: work set for a class, and what each student did with it.

Kept apart from the exam module even though both are "work a student hands in", because they
answer to different rules. An exam has a window, an answer key, a grace period and a
valuation process; homework has a due date and a tick. Modelling homework as a one-question
exam would drag every one of those rules into a teacher setting five sums for tomorrow, and
the teacher would stop using it.

**MISSED is reached by the clock, not by a teacher.** A daily-homework report has to be
answerable the morning after; one that waits for marking measures how busy the teacher is
rather than whether the work was done. `submission_view` therefore derives MISSED at read
time from the due date, and nothing stores it.

**Submissions use a derived document id**, `{assignment_id}:{student_id}`, exactly as exam
submissions do. A student pressing submit twice updates their work rather than filing a
second copy, with no read-then-write race between two tabs.
"""

import logging
from datetime import date, datetime, timedelta

from fastapi import HTTPException

from app.core.enums import HomeworkStatus, UserRole
from app.core.firebase import (
    firestore_classes, firestore_homework, firestore_homework_submissions,
    firestore_student_enrollments, firestore_subjects, firestore_teacher_mappings,
    firestore_users, require_document,
)
from app.services import permissions

logger = logging.getLogger("homework")


def _now() -> str:
    return datetime.utcnow().isoformat()


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def require_assignment(assignment_id) -> dict:
    return require_document(firestore_homework, assignment_id, "Homework assignment")


def submission_id(assignment_id, student_id) -> str:
    return f"{assignment_id}:{student_id}"


# ---------------------------------------------------------------------------------------
# Setting work
# ---------------------------------------------------------------------------------------

def _assert_may_set(class_id, subject_id, actor) -> None:
    """
    A teacher may only set homework for a subject they teach to that class, or any subject
    in a class they lead.

    Checked against the mappings rather than the role, for the reason stated throughout this
    codebase: TEACHER and CLASS_TEACHER pass the same guard, and what separates them is
    recorded per class.
    """
    if getattr(actor, "role", None) == UserRole.ADMIN:
        return
    if permissions.is_class_teacher_of(actor, class_id):
        return

    mine = firestore_teacher_mappings.query_documents("teacher_id", "==", int(actor.id))
    if not any(
        int(m.get("class_id", -1)) == int(class_id)
        and int(m.get("subject_id", -1)) == int(subject_id)
        for m in mine
    ):
        raise HTTPException(
            status_code=403,
            detail="You are not mapped to teach this subject to this class.",
        )


def create_assignment(payload, actor) -> dict:
    require_document(firestore_classes, payload.class_id, "Class")
    require_document(firestore_subjects, payload.subject_id, "Subject")
    _assert_may_set(payload.class_id, payload.subject_id, actor)

    assignment_id = firestore_homework.get_next_numeric_id()
    document = {
        "class_id": int(payload.class_id),
        "subject_id": int(payload.subject_id),
        "teacher_id": int(actor.id),
        "title": payload.title.strip(),
        "description": payload.description,
        "assigned_date": (payload.assigned_date or date.today()).isoformat(),
        "due_date": payload.due_date.isoformat(),
        "max_marks": payload.max_marks,
        "is_mandatory": bool(payload.is_mandatory),
        "allow_late_submission": bool(payload.allow_late_submission),
        "attachments": payload.attachments or [],
        "created_at": _now(),
    }
    firestore_homework.add_document(str(assignment_id), document)
    document["id"] = assignment_id
    logger.info("Homework %s set for class %s, due %s.",
                assignment_id, payload.class_id, document["due_date"])
    return document


def update_assignment(assignment: dict, payload, actor) -> dict:
    """
    Edits an assignment.

    The due date may be moved later freely. Moving it *earlier* past work already handed in
    is refused: those submissions were on time when they were made, and a retroactive
    deadline would mark them late.
    """
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)

    if "due_date" in updates:
        new_due = _as_date(updates["due_date"])
        old_due = _as_date(assignment.get("due_date"))
        if new_due and old_due and new_due < old_due:
            handed_in = [
                s for s in submissions_for(assignment["id"])
                if s.get("submitted_at")
            ]
            if handed_in:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{len(handed_in)} student(s) have already submitted. Moving the "
                        "deadline earlier would mark on-time work late. Extend it, or set "
                        "a new assignment."
                    ),
                )
        updates["due_date"] = new_due.isoformat() if new_due else None

    updates["updated_at"] = _now()
    firestore_homework.add_document(str(assignment["id"]), updates)
    return {**assignment, **updates}


def delete_assignment(assignment: dict) -> int:
    """Removes an assignment and every submission against it. Returns how many went."""
    removed = 0
    for submission in submissions_for(assignment["id"]):
        firestore_homework_submissions.delete_document(str(submission["id"]))
        removed += 1
    firestore_homework.delete_document(str(assignment["id"]))
    return removed


# ---------------------------------------------------------------------------------------
# Doing it
# ---------------------------------------------------------------------------------------

def _class_of(student_id) -> int | None:
    enrollments = firestore_student_enrollments.query_documents(
        "student_id", "==", int(student_id)
    )
    return int(enrollments[0]["class_id"]) if enrollments and enrollments[0].get("class_id") else None


def assert_is_for(assignment: dict, student_id) -> None:
    if int(assignment.get("class_id", -1)) != (_class_of(student_id) or -1):
        raise HTTPException(status_code=404, detail="Homework assignment not found")


def submit(assignment: dict, student_id, payload) -> dict:
    """
    Files a student's work.

    Late submissions are accepted or refused per assignment. When accepted they are flagged
    rather than hidden - a teacher needs to know it was late, and a system that silently
    accepts everything gives the deadline no meaning.
    """
    assert_is_for(assignment, student_id)

    today = date.today()
    due = _as_date(assignment.get("due_date"))
    is_late = bool(due and today > due)

    if is_late and not assignment.get("allow_late_submission", True):
        raise HTTPException(
            status_code=409,
            detail=f"This homework was due on {due} and does not accept late submissions.",
        )

    doc_id = submission_id(assignment["id"], student_id)
    existing = firestore_homework_submissions.get_document(doc_id)

    # Re-submitting after marking would silently invalidate a mark already given and shown
    # to the student. The teacher can clear the grade if they want another attempt.
    if existing and existing.get("status") == HomeworkStatus.GRADED.value:
        raise HTTPException(
            status_code=409,
            detail="This homework has already been marked. Ask your teacher to reopen it "
                   "if you need to resubmit.",
        )

    document = {
        "assignment_id": assignment["id"],
        "student_id": int(student_id),
        "status": HomeworkStatus.SUBMITTED.value,
        "body": payload.body,
        "attachments": payload.attachments or [],
        "submitted_at": _now(),
        "is_late": is_late,
        "created_at": (existing or {}).get("created_at") or _now(),
        "updated_at": _now(),
    }
    firestore_homework_submissions.add_document(doc_id, document)
    document["id"] = doc_id
    return document


def grade(submission: dict, payload, actor) -> dict:
    """Records a mark and feedback."""
    assignment = require_assignment(submission["assignment_id"])
    _assert_may_set(assignment["class_id"], assignment["subject_id"], actor)

    max_marks = assignment.get("max_marks")
    if payload.marks is not None and max_marks is not None and payload.marks > float(max_marks):
        raise HTTPException(
            status_code=400,
            detail=f"{payload.marks} is more than the {max_marks} this homework is out of.",
        )

    updates = {
        "status": HomeworkStatus.GRADED.value,
        "marks": payload.marks,
        "feedback": payload.feedback,
        "graded_by": int(actor.id),
        "graded_at": _now(),
        "updated_at": _now(),
    }
    firestore_homework_submissions.add_document(str(submission["id"]), updates)
    return {**submission, **updates}


def reopen(submission: dict, actor) -> dict:
    """
    Clears a mark so a student may resubmit.

    The counterpart to refusing a resubmission after grading: the teacher decides, rather
    than the student working around it.
    """
    assignment = require_assignment(submission["assignment_id"])
    _assert_may_set(assignment["class_id"], assignment["subject_id"], actor)

    updates = {
        "status": HomeworkStatus.SUBMITTED.value,
        "marks": None,
        "graded_by": None,
        "graded_at": None,
        "updated_at": _now(),
    }
    firestore_homework_submissions.add_document(str(submission["id"]), updates)
    return {**submission, **updates}


# ---------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------

def submissions_for(assignment_id) -> list[dict]:
    return firestore_homework_submissions.query_documents(
        "assignment_id", "==", assignment_id
    )


def submission_of(assignment_id, student_id) -> dict | None:
    return firestore_homework_submissions.get_document(
        submission_id(assignment_id, student_id)
    )


def submission_view(submission: dict | None, assignment: dict,
                    student_id=None, on: date | None = None) -> dict:
    """
    One student's position on one assignment, with MISSED derived from the clock.

    A student who has filed nothing is ASSIGNED until the due date passes and MISSED
    afterwards - neither state is ever written, which is what lets the daily report be right
    at any hour without a sweep having run. See the module docstring.
    """
    today = on or date.today()
    due = _as_date(assignment.get("due_date"))

    if submission is None:
        status = (
            HomeworkStatus.MISSED.value
            if (due and today > due) else HomeworkStatus.ASSIGNED.value
        )
        return {
            "id": submission_id(assignment["id"], student_id),
            "assignment_id": assignment["id"],
            "assignment_title": assignment.get("title"),
            "student_id": int(student_id) if student_id is not None else None,
            "status": status,
            "body": None,
            "attachments": [],
            "submitted_at": None,
            "is_late": False,
            "marks": None,
            "max_marks": assignment.get("max_marks"),
            "feedback": None,
            "graded_by": None,
            "graded_at": None,
        }

    student = firestore_users.get_document(str(submission.get("student_id"))) or {}
    return {
        "id": submission["id"],
        "assignment_id": submission["assignment_id"],
        "assignment_title": assignment.get("title"),
        "student_id": int(submission["student_id"]),
        "student_name": student.get("full_name"),
        "status": submission.get("status") or HomeworkStatus.SUBMITTED.value,
        "body": submission.get("body"),
        "attachments": submission.get("attachments") or [],
        "submitted_at": submission.get("submitted_at"),
        "is_late": bool(submission.get("is_late")),
        "marks": submission.get("marks"),
        "max_marks": assignment.get("max_marks"),
        "feedback": submission.get("feedback"),
        "graded_by": submission.get("graded_by"),
        "graded_at": submission.get("graded_at"),
    }


def students_in_class(class_id) -> list[int]:
    return [
        int(e["student_id"])
        for e in firestore_student_enrollments.list_all()
        if int(e.get("class_id", -1)) == int(class_id) and e.get("student_id") is not None
    ]


def present(assignment: dict, viewer=None, on: date | None = None) -> dict:
    """
    An assignment as the API returns it.

    A teacher gets submission counts; a student gets their own status. Both from the same
    function, because two functions is how the counts and the status end up disagreeing about
    what "submitted" means.
    """
    today = on or date.today()
    due = _as_date(assignment.get("due_date"))

    view = {
        "id": int(assignment["id"]),
        "class_id": assignment.get("class_id"),
        "class_name": (firestore_classes.get_document(str(assignment.get("class_id"))) or {}).get("name"),
        "subject_id": assignment.get("subject_id"),
        "subject_name": (firestore_subjects.get_document(str(assignment.get("subject_id"))) or {}).get("name"),
        "teacher_id": assignment.get("teacher_id"),
        "teacher_name": (firestore_users.get_document(str(assignment.get("teacher_id"))) or {}).get("full_name"),
        "title": assignment.get("title"),
        "description": assignment.get("description"),
        "assigned_date": assignment.get("assigned_date"),
        "due_date": assignment.get("due_date"),
        "max_marks": assignment.get("max_marks"),
        "is_mandatory": bool(assignment.get("is_mandatory", True)),
        "allow_late_submission": bool(assignment.get("allow_late_submission", True)),
        "attachments": assignment.get("attachments") or [],
        "is_overdue": bool(due and today > due),
        "days_until_due": (due - today).days if due else None,
        "created_at": assignment.get("created_at"),
    }

    role = getattr(viewer, "role", None)
    if role == UserRole.STUDENT:
        mine = submission_of(assignment["id"], viewer.id)
        resolved = submission_view(mine, assignment, viewer.id, today)
        view["my_status"] = resolved["status"]
        view["my_marks"] = resolved["marks"]
    elif role is not None:
        handed_in = [s for s in submissions_for(assignment["id"]) if s.get("submitted_at")]
        view["submission_count"] = len(handed_in)
        view["expected_count"] = len(students_in_class(assignment["class_id"]))

    return view


def for_class(class_id, from_date: date | None = None, to_date: date | None = None) -> list[dict]:
    assignments = [
        a for a in firestore_homework.list_all()
        if int(a.get("class_id", -1)) == int(class_id)
    ]
    return _filter_by_date(assignments, from_date, to_date)


def for_teacher(teacher_id, from_date: date | None = None, to_date: date | None = None) -> list[dict]:
    assignments = firestore_homework.query_documents("teacher_id", "==", int(teacher_id))
    return _filter_by_date(assignments, from_date, to_date)


def _filter_by_date(assignments: list[dict], from_date, to_date) -> list[dict]:
    """Filtered on the due date, which is the one anybody plans around."""
    out = []
    for assignment in assignments:
        due = _as_date(assignment.get("due_date"))
        if from_date and (not due or due < from_date):
            continue
        if to_date and (not due or due > to_date):
            continue
        out.append(assignment)
    return sorted(out, key=lambda a: str(a.get("due_date") or ""), reverse=True)
