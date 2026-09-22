"""
Whether the teaching rhythm the school committed to is actually happening.

The brief asks for two obligations: a weekly exam every seven days, and daily homework. This
module answers, for any class and subject, whether they were met - and, when they were not,
how long it has been.

**It reports; it does not block.** Nothing here prevents a teacher doing anything. That is
deliberate: a system that refused to let a teacher take a class because last week's test was
missing would be switched off within a fortnight, and the obligation is a management
question, not a permission one. What it produces is a list an administrator can act on.

**The window is rolling, not calendar.** "Every 7 days" means the gap between consecutive
exams, not one per Monday-to-Sunday block. A calendar week lets a teacher set exams on the
Friday and the following Monday and miss eleven days in between while appearing compliant
both weeks. `days_since` is the honest measure, and it is what the report sorts on.

**Counting is per (class, subject) pair.** A class takes eight subjects and each is a
different teacher's obligation; a report that said "Class 7 had two exams this week" would
hide the six subjects that had none.
"""

import logging
from datetime import date, datetime, timedelta

from app.core.enums import ExamStatus, HomeworkStatus
from app.core.firebase import (
    firestore_classes, firestore_exams, firestore_homework,
    firestore_homework_submissions, firestore_student_enrollments, firestore_subjects,
    firestore_teacher_mappings, firestore_users,
)

logger = logging.getLogger("cadence")

# The obligations, as the brief states them.
EXAM_INTERVAL_DAYS = 7
HOMEWORK_INTERVAL_DAYS = 1


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


def _name(service, doc_id) -> str | None:
    if doc_id is None:
        return None
    record = service.get_document(str(doc_id))
    return (record or {}).get("name") or (record or {}).get("full_name")


# ---------------------------------------------------------------------------------------
# The obligations
# ---------------------------------------------------------------------------------------

def _last_exam(class_id, subject_id, on: date) -> dict | None:
    """
    The most recent published exam for a pair, on or before today.

    Drafts do not count. An unpublished paper is not an exam the class sat, and counting it
    would let the obligation be met by writing something and never releasing it.
    """
    best = None
    best_date = None

    for exam in firestore_exams.list_all():
        if int(exam.get("class_id", -1)) != int(class_id):
            continue
        if int(exam.get("subject_id", -1)) != int(subject_id):
            continue
        if exam.get("status") != ExamStatus.PUBLISHED.value:
            continue

        when = _as_date(exam.get("starts_at")) or _as_date(exam.get("created_at"))
        if not when or when > on:
            continue
        if best_date is None or when > best_date:
            best, best_date = exam, when

    return best


def _last_homework(class_id, subject_id, on: date) -> dict | None:
    """The most recent mandatory homework for a pair, by the date it was set."""
    best = None
    best_date = None

    for assignment in firestore_homework.list_all():
        if int(assignment.get("class_id", -1)) != int(class_id):
            continue
        if int(assignment.get("subject_id", -1)) != int(subject_id):
            continue
        if not assignment.get("is_mandatory", True):
            continue

        when = _as_date(assignment.get("assigned_date"))
        if not when or when > on:
            continue
        if best_date is None or when > best_date:
            best, best_date = assignment, when

    return best


def pair_status(class_id, subject_id, teacher_id=None, on: date | None = None) -> dict:
    """
    Whether one (class, subject) pair is keeping to both obligations.

    `days_since` is null when nothing has ever been set, which is a different problem from
    being late and is reported as such - a subject with no exam ever is a setup gap, not a
    teacher falling behind.
    """
    today = on or date.today()

    exam = _last_exam(class_id, subject_id, today)
    exam_date = _as_date(exam.get("starts_at")) if exam else None
    exam_days = (today - exam_date).days if exam_date else None

    homework = _last_homework(class_id, subject_id, today)
    homework_date = _as_date(homework.get("assigned_date")) if homework else None
    homework_days = (today - homework_date).days if homework_date else None

    return {
        "class_id": int(class_id),
        "class_name": _name(firestore_classes, class_id),
        "subject_id": int(subject_id),
        "subject_name": _name(firestore_subjects, subject_id),
        "teacher_id": int(teacher_id) if teacher_id is not None else None,
        "teacher_name": _name(firestore_users, teacher_id) if teacher_id is not None else None,

        "last_exam_id": exam.get("id") if exam else None,
        "last_exam_title": exam.get("title") if exam else None,
        "last_exam_date": exam_date.isoformat() if exam_date else None,
        "days_since_exam": exam_days,
        "exam_overdue": exam_days is None or exam_days > EXAM_INTERVAL_DAYS,
        "exam_never_set": exam is None,

        "last_homework_id": homework.get("id") if homework else None,
        "last_homework_title": homework.get("title") if homework else None,
        "last_homework_date": homework_date.isoformat() if homework_date else None,
        "days_since_homework": homework_days,
        "homework_overdue": homework_days is None or homework_days > HOMEWORK_INTERVAL_DAYS,
        "homework_never_set": homework is None,
    }


def compliance_report(class_id=None, teacher_id=None, overdue_only: bool = False,
                      on: date | None = None) -> dict:
    """
    Every (class, subject) pair and whether it is keeping up.

    Built from the teacher mappings rather than from the exams: the question is which pairs
    *should* have had an exam, and a list derived from the exams that exist can only ever
    report on the teachers who are already complying.

    Sorted worst first - longest overdue at the top - because this is a list somebody works
    through, not one they read.
    """
    today = on or date.today()

    rows = []
    for mapping in firestore_teacher_mappings.list_all():
        if class_id is not None and int(mapping.get("class_id", -1)) != int(class_id):
            continue
        if teacher_id is not None and int(mapping.get("teacher_id", -1)) != int(teacher_id):
            continue
        if mapping.get("class_id") is None or mapping.get("subject_id") is None:
            continue

        rows.append(pair_status(
            mapping["class_id"], mapping["subject_id"], mapping.get("teacher_id"), today
        ))

    if overdue_only:
        rows = [r for r in rows if r["exam_overdue"] or r["homework_overdue"]]

    # Never-set sorts above merely-late: a subject with no exam at all is the bigger problem,
    # and `days_since` is null for it so it cannot be ordered by that alone.
    rows.sort(key=lambda r: (
        0 if r["exam_never_set"] else 1,
        -(r["days_since_exam"] or 0),
    ))

    return {
        "as_of": today.isoformat(),
        "exam_interval_days": EXAM_INTERVAL_DAYS,
        "homework_interval_days": HOMEWORK_INTERVAL_DAYS,
        "pairs": rows,
        "total_pairs": len(rows),
        "exam_overdue_count": sum(1 for r in rows if r["exam_overdue"]),
        "homework_overdue_count": sum(1 for r in rows if r["homework_overdue"]),
        "never_examined_count": sum(1 for r in rows if r["exam_never_set"]),
    }


# ---------------------------------------------------------------------------------------
# Per-student counts - "weekly exams counts by students to admin"
# ---------------------------------------------------------------------------------------

def student_exam_counts(class_id=None, from_date: date | None = None,
                        to_date: date | None = None) -> dict:
    """
    How many weekly exams each student actually sat, against how many were set for them.

    The brief asks for exam counts by student, reported to the admin, and the useful figure
    is the pair: a student who sat 3 of 8 and one who sat 3 of 3 have the same count and
    opposite problems. `attempted` counts submissions of any status - a script handed in and
    not yet marked is still a paper the student sat.

    Defaults to the last four weeks, which is the span an administrator reviewing a term
    actually asks about.
    """
    to_date = to_date or date.today()
    from_date = from_date or (to_date - timedelta(days=28))

    # Which published exams fall in the window, grouped by the class they were set for.
    exams_by_class: dict[int, list[dict]] = {}
    for exam in firestore_exams.list_all():
        if exam.get("status") != ExamStatus.PUBLISHED.value:
            continue
        when = _as_date(exam.get("starts_at")) or _as_date(exam.get("created_at"))
        if not when or not (from_date <= when <= to_date):
            continue
        key = exam.get("class_id")
        if key is None:
            continue
        exams_by_class.setdefault(int(key), []).append(exam)

    from app.core.firebase import firestore_exam_submissions

    rows = []
    for enrollment in firestore_student_enrollments.list_all():
        student_id = enrollment.get("student_id")
        enrolled_class = enrollment.get("class_id")
        if student_id is None or enrolled_class is None:
            continue
        if class_id is not None and int(enrolled_class) != int(class_id):
            continue

        student = firestore_users.get_document(str(student_id))
        if not student or not student.get("is_active", True):
            continue

        set_for_them = exams_by_class.get(int(enrolled_class), [])
        exam_ids = {str(e["id"]) for e in set_for_them}

        mine = [
            s for s in firestore_exam_submissions.query_documents(
                "student_id", "==", int(student_id)
            )
            if str(s.get("exam_id")) in exam_ids
        ]
        attempted = [s for s in mine if s.get("status") != "MISSED"]

        expected = len(set_for_them)
        rows.append({
            "student_id": int(student_id),
            "student_name": student.get("full_name"),
            "admission_number": student.get("admission_number"),
            "class_id": int(enrolled_class),
            "class_name": _name(firestore_classes, enrolled_class),
            "exams_set": expected,
            "exams_attempted": len(attempted),
            "exams_missed": max(expected - len(attempted), 0),
            "attendance_percent": (
                round(len(attempted) * 100.0 / expected, 1) if expected else None
            ),
        })

    # Worst attendance first: this is a list for chasing students, so the ones who need
    # chasing belong at the top. Students with no exams set sort last - nothing to chase.
    rows.sort(key=lambda r: (
        r["attendance_percent"] if r["attendance_percent"] is not None else 101,
        -r["exams_missed"],
    ))

    return {
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "students": rows,
        "total_students": len(rows),
        "students_with_missed": sum(1 for r in rows if r["exams_missed"] > 0),
    }


def student_homework_counts(class_id=None, from_date: date | None = None,
                            to_date: date | None = None) -> dict:
    """
    The same picture for daily homework.

    MISSED is derived here exactly as `homework.submission_view` derives it - from the due
    date having passed with nothing filed - so the report and the student's own screen agree
    without either reading a stored status that some sweep has to maintain.
    """
    to_date = to_date or date.today()
    from_date = from_date or (to_date - timedelta(days=7))
    today = date.today()

    assignments_by_class: dict[int, list[dict]] = {}
    for assignment in firestore_homework.list_all():
        if not assignment.get("is_mandatory", True):
            continue
        due = _as_date(assignment.get("due_date"))
        if not due or not (from_date <= due <= to_date):
            continue
        key = assignment.get("class_id")
        if key is None:
            continue
        assignments_by_class.setdefault(int(key), []).append(assignment)

    rows = []
    for enrollment in firestore_student_enrollments.list_all():
        student_id = enrollment.get("student_id")
        enrolled_class = enrollment.get("class_id")
        if student_id is None or enrolled_class is None:
            continue
        if class_id is not None and int(enrolled_class) != int(class_id):
            continue

        student = firestore_users.get_document(str(student_id))
        if not student or not student.get("is_active", True):
            continue

        set_for_them = assignments_by_class.get(int(enrolled_class), [])
        submitted = late = missed = 0

        for assignment in set_for_them:
            record = firestore_homework_submissions.get_document(
                f"{assignment['id']}:{student_id}"
            )
            if record and record.get("submitted_at"):
                submitted += 1
                if record.get("is_late"):
                    late += 1
            else:
                due = _as_date(assignment.get("due_date"))
                if due and today > due:
                    missed += 1

        expected = len(set_for_them)
        rows.append({
            "student_id": int(student_id),
            "student_name": student.get("full_name"),
            "class_id": int(enrolled_class),
            "class_name": _name(firestore_classes, enrolled_class),
            "homework_set": expected,
            "submitted": submitted,
            "late": late,
            "missed": missed,
            "submission_percent": (
                round(submitted * 100.0 / expected, 1) if expected else None
            ),
        })

    rows.sort(key=lambda r: (
        r["submission_percent"] if r["submission_percent"] is not None else 101,
        -r["missed"],
    ))

    return {
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "students": rows,
        "total_students": len(rows),
        "students_with_missed": sum(1 for r in rows if r["missed"] > 0),
    }
