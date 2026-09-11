"""
Tuition assessments: homework, assignments and exams set for one student.

This module is deliberately thin, and that is the point. The brief asks for "the same exam
module with grading and cards" - so this does not reimplement any of it. Question forms,
answer keys, timed windows, per-student concessions, auto-marking, valuation, late uploads,
publishing: every one of those lives in `app.services.exams` and works here unchanged.

What this module supplies is the two things that genuinely differ:

  * **Who it is for.** An LMS exam is addressed to a class and its roster is the enrollment
    list. A tuition assessment is addressed to one student on one arrangement. `roster_for`
    in the exam service resolves both, and everything downstream - who may open the paper,
    whose script is expected, who appears on the result sheet - follows from it.
  * **What it is called.** A `category` of HOMEWORK, ASSIGNMENT or EXAM. One-to-one teaching
    sets far more homework than exams, and a student's list that cannot tell the two apart
    is unusable. It changes no behaviour; it is a label, and it is honest about being one.

Report cards are built here too, from the same submissions, because a tuition card spans a
student's subjects rather than a class's - see `generate_report_card`.
"""

import logging

from fastapi import HTTPException

from app.core.enums import ExamStatus, Program
from app.core.firebase import (
    firestore_exams, firestore_report_cards, firestore_subjects, firestore_users,
    generate_id,
)
from app.services.exams import (
    build_questions, grade_for, now_utc, questions_total, store_dt, submissions_for_exam,
)
from app.services.report_cards import _common_bands, _counts_toward_total, _exam_line
from app.services.tuition.common import is_admin, parse_date, user_id_of
from app.services.tuition.enrollments import require_enrollment
from app.services.tuition.reports import student_report

logger = logging.getLogger("tuition.assessments")

CATEGORIES = frozenset({"HOMEWORK", "ASSIGNMENT", "EXAM", "TEST", "PROJECT"})


# ---------------------------------------------------------------------------------------
# Setting work
# ---------------------------------------------------------------------------------------

def create_assessment(payload, actor) -> dict:
    """
    Sets a piece of work for one student.

    The student, teacher and subject all come from the enrollment rather than from the
    request. A teacher cannot set homework for a student they do not teach, because the only
    place those ids come from is the arrangement, and the arrangement is checked first.

    The document shape is the LMS exam shape plus four fields, which is what lets the rest of
    the exam engine operate on it without knowing tuition exists.
    """
    enrollment = require_enrollment(payload.enrollment_id)

    actor_id = user_id_of(actor)
    if not is_admin(actor) and actor_id != enrollment.get("teacher_id"):
        raise HTTPException(
            status_code=403,
            detail="Only the teacher assigned to this subject may set work for this student.",
        )

    category = (payload.category or "HOMEWORK").upper()
    if category not in CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"category must be one of {sorted(CATEGORIES)}.",
        )

    questions = build_questions(payload.questions)
    max_marks = payload.max_marks or questions_total(questions) or 100.0
    if payload.pass_marks is not None and payload.pass_marks > max_marks:
        raise HTTPException(
            status_code=400,
            detail=f"pass_marks ({payload.pass_marks}) cannot exceed max_marks ({max_marks}).",
        )

    exam_id = generate_id()
    document = {
        # What makes this a tuition assessment. `class_id` stays null: there is no class.
        "program": Program.TUITION.value,
        "category": category,
        "enrollment_id": enrollment["id"],
        "student_id": enrollment["student_id"],
        "class_id": None,

        "subject_id": enrollment["subject_id"],
        "teacher_id": enrollment["teacher_id"],
        "created_by": actor_id,
        "title": payload.title,
        "description": payload.description,
        "instructions": payload.instructions,
        "mode": payload.mode.value,
        "status": payload.status.value,

        "grading_scheme": payload.grading_scheme.value,
        "max_marks": float(max_marks),
        "pass_marks": payload.pass_marks,
        "grade_bands": [b.model_dump() for b in sorted(
            payload.grade_bands, key=lambda b: b.min_percentage, reverse=True
        )],

        "starts_at": store_dt(payload.starts_at),
        "ends_at": store_dt(payload.ends_at),
        "duration_minutes": payload.duration_minutes,
        "upload_grace_minutes": payload.upload_grace_minutes,
        "late_submission_allowed": payload.late_submission_allowed,
        "time_concessions": {},

        "questions": questions,
        "shuffle_questions": payload.shuffle_questions,
        "auto_grade_objective": payload.auto_grade_objective,

        "question_paper_url": None,
        "question_paper_provider": None,
        "question_paper_warning": None,
        "max_upload_files": payload.max_upload_files,

        "results_published": False,
        "results_published_at": None,
        "created_at": now_utc().isoformat(),
        "updated_at": None,
    }

    firestore_exams.add_document(str(exam_id), document)
    document["id"] = exam_id
    logger.info("Tuition %s %s set for student %s on enrollment %s.",
                category.lower(), exam_id, enrollment["student_id"], enrollment["id"])
    return document


# ---------------------------------------------------------------------------------------
# Finding work
# ---------------------------------------------------------------------------------------

def for_student(student_id: int, include_drafts: bool = False) -> list[dict]:
    """
    A student's tuition work.

    Drafts are excluded by default and there is no way for a student to ask for them: an
    unpublished paper must be invisible, not merely unlisted, or the id is the only thing
    between a student and tomorrow's questions.
    """
    records = [
        e for e in firestore_exams.query_documents("student_id", "==", student_id)
        if e.get("program") == Program.TUITION.value
    ]
    if include_drafts:
        return sort_assessments(records)
    return sort_assessments([
        e for e in records if e.get("status") == ExamStatus.PUBLISHED.value
    ])


def for_teacher(teacher_id: int) -> list[dict]:
    return sort_assessments([
        e for e in firestore_exams.query_documents("teacher_id", "==", teacher_id)
        if e.get("program") == Program.TUITION.value
    ])


def for_enrollment(enrollment_id) -> list[dict]:
    return sort_assessments([
        e for e in firestore_exams.query_documents("enrollment_id", "==", enrollment_id)
        if e.get("program") == Program.TUITION.value
    ])


def all_assessments() -> list[dict]:
    return sort_assessments([
        e for e in firestore_exams.query_documents("program", "==", Program.TUITION.value)
    ])


def sort_assessments(records: list[dict]) -> list[dict]:
    """Nearest deadline first: the thing a student needs to do next belongs at the top."""
    return sorted(records, key=lambda e: str(e.get("ends_at") or ""))


def filter_assessments(records: list[dict], category: str | None = None,
                       subject_id: int | None = None, status: str | None = None) -> list[dict]:
    results = records
    if category:
        results = [r for r in results if r.get("category") == category.upper()]
    if subject_id is not None:
        results = [r for r in results if r.get("subject_id") == subject_id]
    if status:
        results = [r for r in results if r.get("status") == status]
    return results


def assert_may_manage(exam: dict, actor) -> None:
    """The assigned teacher, or an admin. Nobody else edits or marks a private lesson's work."""
    if is_admin(actor):
        return
    if user_id_of(actor) != exam.get("teacher_id"):
        raise HTTPException(status_code=403, detail="Only the assigned teacher may manage this work.")


def assert_is_tuition(exam: dict) -> None:
    """
    Refuses an LMS exam id on a tuition endpoint.

    Both products share the `exams` collection, so without this a tuition route would happily
    operate on a school exam - and its role guard, which only knows about tuition, would
    have no idea it had been handed the wrong product.
    """
    if exam.get("program") != Program.TUITION.value:
        raise HTTPException(status_code=404, detail="Assessment not found")


# ---------------------------------------------------------------------------------------
# Report cards
# ---------------------------------------------------------------------------------------

def report_card_id(student_id, title: str) -> str:
    """
    Derived from student and title so re-issuing a card corrects it rather than adding a
    second one - the same rule the LMS card uses, minus the class it does not have.
    """
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", str(title).strip().lower()).strip("-") or "card"
    return f"tuition_{student_id}_{slug}"


def generate_report_card(payload, actor) -> dict:
    """
    Consolidates a student's tuition work into one card.

    Structurally the same document the LMS issues, and built with the same helpers - the
    marks arithmetic, the grade bands, and the rule that a missed paper is not a zero unless
    somebody says so, all reused rather than restated.

    Two things differ, both because tuition has no class. There is no rank and no class size:
    a one-to-one student has no cohort, and inventing one by ranking them against other
    people's private students would be meaningless as well as intrusive. And the attendance
    figure comes from tuition sessions rather than the LMS attendance collection, which is
    the number a parent actually wants beside the marks.
    """
    student = firestore_users.get_document(str(payload.student_id))
    if not student:
        raise HTTPException(status_code=404, detail=f"Student {payload.student_id} not found")

    from_date = parse_date(payload.from_date)
    to_date = parse_date(payload.to_date)

    exams = [
        e for e in for_student(payload.student_id, include_drafts=True)
        if e.get("status") == ExamStatus.PUBLISHED.value and _in_window(e, from_date, to_date)
    ]
    bands = _common_bands(exams)

    submissions: dict = {}
    for exam in exams:
        for submission in submissions_for_exam(exam["id"]):
            if submission.get("student_id") == payload.student_id:
                submissions[exam["id"]] = submission

    by_subject: dict = {}
    total_marks = total_max = 0.0
    counted = missed = 0

    for exam in exams:
        line = _exam_line(exam, submissions.get(exam["id"]), payload.count_missing_as_zero)
        line["category"] = exam.get("category")

        subject_id = exam.get("subject_id")
        subject = firestore_subjects.get_document(str(subject_id)) or {}
        block = by_subject.setdefault(subject_id, {
            "subject_id": subject_id,
            "subject_name": subject.get("name"),
            "subject_code": subject.get("code"),
            "exams": [],
            "total_marks": 0.0,
            "total_max_marks": 0.0,
            "percentage": None,
            "grade": None,
            "exams_counted": 0,
            "exams_missed": 0,
            "teacher_remarks": None,
        })
        block["exams"].append(line)

        if line["missed"]:
            block["exams_missed"] += 1
            missed += 1

        if _counts_toward_total(line, exam, payload.count_missing_as_zero):
            marks = float(line["marks_obtained"])
            max_marks = float(exam.get("max_marks") or 0.0)
            block["total_marks"] += marks
            block["total_max_marks"] += max_marks
            block["exams_counted"] += 1
            total_marks += marks
            total_max += max_marks
            counted += 1

    for block in by_subject.values():
        block["total_marks"] = round(block["total_marks"], 2)
        block["total_max_marks"] = round(block["total_max_marks"], 2)
        if block["total_max_marks"] > 0:
            block["percentage"] = round(block["total_marks"] / block["total_max_marks"] * 100.0, 2)
            block["grade"] = grade_for(block["percentage"], bands)

    overall = round(total_marks / total_max * 100.0, 2) if total_max > 0 else 0.0
    attendance = student_report(payload.student_id, from_date, to_date)["totals"]

    document = {
        "program": Program.TUITION.value,
        "student_id": payload.student_id,
        "class_id": None,
        "title": payload.title,
        "generated_by": user_id_of(actor),
        "generated_at": now_utc().isoformat(),
        "from_date": from_date.isoformat() if from_date else None,
        "to_date": to_date.isoformat() if to_date else None,
        "subjects": sorted(by_subject.values(), key=lambda b: (b["subject_name"] or "")),
        "total_marks": round(total_marks, 2),
        "total_max_marks": round(total_max, 2),
        "overall_percentage": overall,
        "overall_grade": grade_for(overall, bands) if total_max > 0 else None,
        "exams_counted": counted,
        "exams_missed": missed,
        "attendance_percentage": attendance["attendance_percentage"],
        "classes_conducted": attendance["conducted"],
        "classes_attended": attendance["attended"],
        # No cohort, so no rank. See the docstring.
        "rank": None,
        "class_size": None,
        "remarks": payload.remarks,
        "is_published": False,
        "published_at": None,
    }

    document_id = report_card_id(payload.student_id, payload.title)
    firestore_report_cards.add_document(document_id, document)
    document["id"] = document_id
    return document


def _in_window(exam: dict, from_date, to_date) -> bool:
    from app.services.exams import to_utc

    ends = to_utc(exam.get("ends_at"))
    if ends is None:
        return True
    on_date = ends.date()
    if from_date and on_date < from_date:
        return False
    if to_date and on_date > to_date:
        return False
    return True


def cards_for_student(student_id: int, published_only: bool = False) -> list[dict]:
    records = [
        c for c in firestore_report_cards.query_documents("student_id", "==", student_id)
        if c.get("program") == Program.TUITION.value
    ]
    if published_only:
        records = [c for c in records if c.get("is_published")]
    return sorted(records, key=lambda c: str(c.get("generated_at") or ""), reverse=True)


def publish_card(card: dict) -> dict:
    """
    Releases a card to the student.

    Separate from generation because a teacher builds a card, reads it, adds a remark, and
    only then hands it over. Generating and publishing in one step would put every draft in
    front of the family it is about.
    """
    updates = {
        "is_published": True,
        "published_at": now_utc().isoformat(),
        "updated_at": now_utc().isoformat(),
    }
    firestore_report_cards.add_document(str(card["id"]), updates)
    return {**card, **updates}
