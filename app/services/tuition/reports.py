"""
Tuition attendance and class-count reporting.

Three audiences, one set of arithmetic, three different scopes - and the scoping is the part
that matters, because the brief is explicit that teachers and students see **only their
own**. Getting that wrong in a one-to-one product is not a leaked statistic, it is one
family's private arrangement shown to another.

  * A **student** sees their own classes, per subject.
  * A **teacher** sees the classes they took, per student.
  * An **admin** sees everybody, and it is their counts the fee module prices.

The counting rules are shared, and they are worth reading once because they are the bridge
between attendance and money:

  * **Conducted** - the class happened. COMPLETED, or NO_SHOW_STUDENT: the teacher was there
    and the hour was spent whether or not the student used it.
  * **Attended** - the student was actually present (or late, which is present).
  * **Missed** - the class happened without them.
  * **Teacher no-show** and **cancelled** are counted but conducted by nobody, which is why
    they are reported separately rather than folded into a percentage that would quietly
    charge a student for a class their teacher missed.
"""

import logging
from datetime import date, timedelta

from app.core.enums import AttendanceStatus, TuitionSessionStatus
from app.core.firebase import (
    firestore_subjects, firestore_tuition_sessions, firestore_users, prefetch_tuition,
)
from app.services.tuition.common import parse_date, program_timezone
from app.services.tuition.enrollments import for_student, for_teacher
from app.services.tuition.sessions import filter_sessions, is_billable, list_for_person

logger = logging.getLogger("tuition.reports")

PRESENT_VALUES = frozenset({AttendanceStatus.PRESENT.value, AttendanceStatus.LATE.value})


def default_period(days: int = 30) -> tuple[date, date]:
    """The last `days` days in programme time, for a report called with no dates."""
    from datetime import datetime
    today = datetime.now(program_timezone()).date()
    return today - timedelta(days=days), today


def summarize(sessions: list[dict]) -> dict:
    """
    The counts every report is built from.

    `attendance_percentage` is deliberately measured against *conducted* classes rather than
    scheduled ones. A student whose teacher missed two classes has an attendance record of
    100%, not 60% - punishing them in a statistic for somebody else's absence is both unfair
    and, since these counts price the invoices, expensive.
    """
    total = len(sessions)
    conducted = attended = missed = late = cancelled = teacher_no_show = upcoming = 0
    billable = 0
    taught_minutes = 0.0
    scheduled_minutes = 0.0

    for session in sessions:
        status = session.get("status")
        attendance = session.get("attendance_status")
        scheduled_minutes += float(session.get("duration_minutes") or 0)

        if status == TuitionSessionStatus.CANCELLED.value:
            cancelled += 1
            continue
        if status == TuitionSessionStatus.NO_SHOW_TEACHER.value:
            teacher_no_show += 1
            continue
        if status in {TuitionSessionStatus.SCHEDULED.value, TuitionSessionStatus.IN_PROGRESS.value}:
            upcoming += 1
            continue

        conducted += 1
        taught_minutes += float(session.get("actual_duration_minutes") or 0)
        if is_billable(session):
            billable += 1

        if attendance in PRESENT_VALUES:
            attended += 1
            if attendance == AttendanceStatus.LATE.value:
                late += 1
        elif attendance == AttendanceStatus.EXCUSED.value:
            # Excused absences are neither attended nor held against the student.
            pass
        else:
            missed += 1

    return {
        "total_sessions": total,
        "conducted": conducted,
        "attended": attended,
        "late": late,
        "missed": missed,
        "cancelled": cancelled,
        "teacher_no_show": teacher_no_show,
        "upcoming": upcoming,
        "billable_sessions": billable,
        "attendance_percentage": round(attended / conducted * 100, 2) if conducted else None,
        "scheduled_minutes": round(scheduled_minutes, 2),
        "taught_minutes": round(taught_minutes, 2),
    }


def _group(sessions: list[dict], key: str) -> dict:
    grouped: dict = {}
    for session in sessions:
        grouped.setdefault(session.get(key), []).append(session)
    return grouped


def student_report(student_id: int, from_date=None, to_date=None) -> dict:
    """
    One student's attendance, broken down by subject.

    Per subject rather than per enrollment in the output, because that is the question a
    parent asks - "how is she doing in physics" - even though the underlying scope is the
    enrollment.
    """
    start, end = _window(from_date, to_date)
    sessions = filter_sessions(list_for_person(student_id, as_teacher=False), start, end)
    prefetch_tuition(sessions)

    student = firestore_users.get_document(str(student_id)) or {}
    enrollments = {str(e.get("id")): e for e in for_student(student_id, include_inactive=True)}

    subjects = []
    for enrollment_id, group in _group(sessions, "enrollment_id").items():
        enrollment = enrollments.get(str(enrollment_id), {})
        subject = firestore_subjects.get_document(str(group[0].get("subject_id"))) or {}
        teacher = firestore_users.get_document(str(group[0].get("teacher_id"))) or {}
        subjects.append({
            "enrollment_id": enrollment_id,
            "subject_id": group[0].get("subject_id"),
            "subject_name": subject.get("name"),
            "teacher_id": group[0].get("teacher_id"),
            "teacher_name": teacher.get("full_name"),
            "enrollment_status": enrollment.get("status"),
            **summarize(group),
        })
    subjects.sort(key=lambda row: str(row.get("subject_name") or ""))

    return {
        "student_id": student_id,
        "student_name": student.get("full_name"),
        "admission_number": student.get("admission_number"),
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "totals": summarize(sessions),
        "subjects": subjects,
    }


def teacher_report(teacher_id: int, from_date=None, to_date=None) -> dict:
    """
    One teacher's classes, broken down by student.

    The teacher-side counterpart, and the one the admin prices a teacher's payment from:
    `conducted` is how many classes they actually took, and `teacher_no_show` is how many
    they did not.
    """
    start, end = _window(from_date, to_date)
    sessions = filter_sessions(list_for_person(teacher_id, as_teacher=True), start, end)
    prefetch_tuition(sessions)

    teacher = firestore_users.get_document(str(teacher_id)) or {}
    enrollments = {str(e.get("id")): e for e in for_teacher(teacher_id, include_inactive=True)}

    students = []
    for enrollment_id, group in _group(sessions, "enrollment_id").items():
        enrollment = enrollments.get(str(enrollment_id), {})
        subject = firestore_subjects.get_document(str(group[0].get("subject_id"))) or {}
        student = firestore_users.get_document(str(group[0].get("student_id"))) or {}
        students.append({
            "enrollment_id": enrollment_id,
            "student_id": group[0].get("student_id"),
            "student_name": student.get("full_name"),
            "admission_number": student.get("admission_number"),
            "subject_id": group[0].get("subject_id"),
            "subject_name": subject.get("name"),
            "enrollment_status": enrollment.get("status"),
            **summarize(group),
        })
    students.sort(key=lambda row: str(row.get("student_name") or ""))

    return {
        "teacher_id": teacher_id,
        "teacher_name": teacher.get("full_name"),
        "employee_id": teacher.get("employee_id"),
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "totals": summarize(sessions),
        "students": students,
    }


def programme_report(from_date=None, to_date=None) -> dict:
    """
    Every student and every teacher over a period. Admin only, and the input to the fees run.

    Built from one full read of the sessions in the window rather than one report per person.
    A programme with sixty students would otherwise cost sixty queries to answer a question
    that is genuinely one query, and this is the endpoint an admin refreshes most.
    """
    start, end = _window(from_date, to_date)
    sessions = filter_sessions(firestore_tuition_sessions.list_all(), start, end)
    prefetch_tuition(sessions)

    students = []
    for student_id, group in _group(sessions, "student_id").items():
        person = firestore_users.get_document(str(student_id)) or {}
        students.append({
            "student_id": student_id,
            "student_name": person.get("full_name"),
            "admission_number": person.get("admission_number"),
            "subjects_taken": len({s.get("enrollment_id") for s in group}),
            **summarize(group),
        })

    teachers = []
    for teacher_id, group in _group(sessions, "teacher_id").items():
        person = firestore_users.get_document(str(teacher_id)) or {}
        teachers.append({
            "teacher_id": teacher_id,
            "teacher_name": person.get("full_name"),
            "employee_id": person.get("employee_id"),
            "students_taught": len({s.get("student_id") for s in group}),
            **summarize(group),
        })

    students.sort(key=lambda row: str(row.get("student_name") or ""))
    teachers.sort(key=lambda row: str(row.get("teacher_name") or ""))

    return {
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "totals": summarize(sessions),
        "students": students,
        "teachers": teachers,
    }


def enrollment_counts(enrollment_id, from_date, to_date) -> dict:
    """Session counts for one arrangement over a period. What an invoice line is priced from."""
    sessions = filter_sessions(
        firestore_tuition_sessions.query_documents("enrollment_id", "==", enrollment_id),
        from_date, to_date,
    )
    return summarize(sessions)


def _window(from_date, to_date) -> tuple[date, date]:
    start = parse_date(from_date)
    end = parse_date(to_date)
    if start and end:
        return start, end
    default_start, default_end = default_period()
    return start or default_start, end or default_end
