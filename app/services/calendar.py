"""
One calendar per person, gathering everything that happens on a date.

The brief asks for a tile view and a calendar view for teachers and students. Both are the
same data laid out differently, so this returns events and lets the client choose - a
calendar endpoint that returned pre-grouped weeks would need a second endpoint the moment
somebody wanted a list.

What it gathers, per role:

  * **timetable periods** - the recurring weekly pattern, expanded to real dates
  * **live classes** - school meetings and tuition sessions
  * **exams** - published ones only; a draft paper is not an event yet
  * **homework** - shown on its due date, which is the date that matters to a student
  * **leave** - a teacher's own approved absence, so their calendar is honest

Every event is flattened to the same shape (`_event`). That is the point of the module: a
frontend rendering a week should not have to know that a timetable period and a tuition
session come from collections that share no fields. The `kind` discriminates for colour and
icon; everything else is uniform.

**Expansion is bounded.** A range longer than `MAX_RANGE_DAYS` is refused rather than
silently truncated, because expanding a recurring timetable across a year is thousands of
synthesised events and the request that asks for it is nearly always a client bug.
"""

import logging
from datetime import date, datetime, time, timedelta

from fastapi import HTTPException

from app.core.enums import DayOfWeek, ExamStatus, Program, UserRole
from app.core.firebase import (
    firestore_classes, firestore_exams, firestore_homework, firestore_meetings,
    firestore_student_enrollments, firestore_subjects, firestore_timetable,
    firestore_tuition_enrollments, firestore_tuition_sessions, firestore_users,
)
from app.services import permissions

logger = logging.getLogger("calendar")

# Expanding a weekly timetable over a long range synthesises an event per period per day.
# Ninety days is a generous quarter view; beyond that the caller wants a report, not a
# calendar.
MAX_RANGE_DAYS = 90


def _as_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


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


def _as_time(value) -> time | None:
    if isinstance(value, time):
        return value
    if value is None:
        return None
    try:
        return time.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _event(kind: str, title: str, start: datetime | None, **extra) -> dict:
    """
    One calendar entry, in the shape every source is flattened to.

    `start` may be None for an all-day item such as homework due on a date with no time;
    the client renders those in a day's header rather than at an hour.
    """
    return {
        "kind": kind,
        "title": title,
        "start_at": start.isoformat() if start else None,
        "end_at": None,
        "all_day": start is None,
        "class_id": None,
        "class_name": None,
        "subject_id": None,
        "subject_name": None,
        "teacher_id": None,
        "teacher_name": None,
        "student_id": None,
        "student_name": None,
        "status": None,
        "meeting_link": None,
        "reference_id": None,
        "program": Program.LMS.value,
        "is_cancelled": False,
        **extra,
    }


def _name_of(service, doc_id) -> str | None:
    if doc_id is None:
        return None
    record = service.get_document(str(doc_id))
    return (record or {}).get("name") or (record or {}).get("full_name")


def _assert_range(from_date: date, to_date: date) -> None:
    if to_date < from_date:
        raise HTTPException(status_code=400, detail="to_date must fall on or after from_date.")
    if (to_date - from_date).days > MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"A calendar range may span at most {MAX_RANGE_DAYS} days; "
                   f"{(to_date - from_date).days} were requested.",
        )


# ---------------------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------------------

def _timetable_events(entries: list[dict], from_date: date, to_date: date,
                      viewer_role) -> list[dict]:
    """
    Expands recurring weekly periods into concrete dated events.

    The recurrence is the whole reason this cannot be a query: a timetable entry says
    "Tuesdays at 09:00", and a calendar needs the four Tuesdays in the range. Grouping the
    entries by weekday first means the expansion walks the dates once rather than scanning
    every entry per day.
    """
    by_weekday: dict[int, list[dict]] = {}
    for entry in entries:
        try:
            weekday = DayOfWeek(entry.get("day_of_week")).iso_weekday
        except (ValueError, TypeError):
            continue
        by_weekday.setdefault(weekday, []).append(entry)

    events = []
    cursor = from_date
    while cursor <= to_date:
        for entry in by_weekday.get(cursor.isoweekday(), []):
            start_time = _as_time(entry.get("start_time"))
            end_time = _as_time(entry.get("end_time"))
            start_at = datetime.combine(cursor, start_time) if start_time else None

            events.append(_event(
                "TIMETABLE",
                entry.get("title") or _name_of(firestore_subjects, entry.get("subject_id"))
                or "Period",
                start_at,
                end_at=(
                    datetime.combine(cursor, end_time).isoformat() if end_time else None
                ),
                class_id=entry.get("class_id"),
                class_name=_name_of(firestore_classes, entry.get("class_id")),
                subject_id=entry.get("subject_id"),
                subject_name=_name_of(firestore_subjects, entry.get("subject_id")),
                teacher_id=entry.get("teacher_id"),
                teacher_name=_name_of(firestore_users, entry.get("teacher_id")),
                reference_id=entry.get("id"),
            ))
        cursor += timedelta(days=1)
    return events


def _meeting_events(meetings: list[dict], from_date: date, to_date: date) -> list[dict]:
    from app.services import live_classes

    events = []
    for meeting in meetings:
        start = _as_datetime(meeting.get("scheduled_time"))
        if not start or not (from_date <= start.date() <= to_date):
            continue

        timing = live_classes.timing_view(meeting)
        events.append(_event(
            "LIVE_CLASS",
            meeting.get("title") or "Live class",
            start,
            end_at=timing["scheduled_end_at"],
            class_id=meeting.get("class_id"),
            class_name=_name_of(firestore_classes, meeting.get("class_id")),
            subject_id=meeting.get("subject_id"),
            subject_name=_name_of(firestore_subjects, meeting.get("subject_id")),
            teacher_id=meeting.get("teacher_id"),
            teacher_name=_name_of(firestore_users, meeting.get("teacher_id")),
            status=meeting.get("status"),
            # The link is only handed over once the class is actually joinable, so a
            # calendar cannot be used to walk into a room early - the same rule the join
            # endpoint enforces, applied at the point the link would otherwise leak.
            meeting_link=meeting.get("meeting_link") if timing["may_join"] else None,
            reference_id=meeting.get("id"),
            is_cancelled=meeting.get("status") == "CANCELLED",
            timing=timing,
        ))
    return events


def _tuition_events(sessions: list[dict], from_date: date, to_date: date) -> list[dict]:
    events = []
    for session in sessions:
        start = _as_datetime(session.get("scheduled_start_at"))
        if not start or not (from_date <= start.date() <= to_date):
            continue

        enrollment = firestore_tuition_enrollments.get_document(
            str(session.get("enrollment_id"))
        ) or {}
        events.append(_event(
            "TUITION_CLASS",
            session.get("topic") or _name_of(firestore_subjects, enrollment.get("subject_id"))
            or "Tuition class",
            start,
            end_at=(
                _as_datetime(session.get("scheduled_end_at")).isoformat()
                if _as_datetime(session.get("scheduled_end_at")) else None
            ),
            subject_id=enrollment.get("subject_id"),
            subject_name=_name_of(firestore_subjects, enrollment.get("subject_id")),
            teacher_id=enrollment.get("teacher_id"),
            teacher_name=_name_of(firestore_users, enrollment.get("teacher_id")),
            student_id=enrollment.get("student_id"),
            student_name=_name_of(firestore_users, enrollment.get("student_id")),
            status=session.get("status"),
            meeting_link=session.get("meeting_link"),
            reference_id=session.get("id"),
            program=Program.TUITION.value,
            is_cancelled=session.get("status") == "CANCELLED",
        ))
    return events


def _exam_events(exams: list[dict], from_date: date, to_date: date) -> list[dict]:
    events = []
    for exam in exams:
        # Drafts are invisible: an unpublished paper is not an event, and showing one on a
        # student's calendar would leak a test the teacher has not released.
        if exam.get("status") != ExamStatus.PUBLISHED.value:
            continue

        start = _as_datetime(exam.get("starts_at"))
        if not start or not (from_date <= start.date() <= to_date):
            continue

        events.append(_event(
            "EXAM",
            exam.get("title") or "Exam",
            start,
            end_at=(
                _as_datetime(exam.get("ends_at")).isoformat()
                if _as_datetime(exam.get("ends_at")) else None
            ),
            class_id=exam.get("class_id"),
            class_name=_name_of(firestore_classes, exam.get("class_id")),
            subject_id=exam.get("subject_id"),
            subject_name=_name_of(firestore_subjects, exam.get("subject_id")),
            teacher_id=exam.get("teacher_id"),
            status=exam.get("status"),
            reference_id=exam.get("id"),
            is_cancelled=exam.get("status") == ExamStatus.CANCELLED.value,
        ))
    return events


def _homework_events(assignments: list[dict], from_date: date, to_date: date) -> list[dict]:
    """
    Homework, shown on the day it is due.

    The due date rather than the set date, because that is the one a student plans around -
    a calendar showing when work was handed out tells them nothing they need.
    """
    events = []
    for assignment in assignments:
        due = _as_date(assignment.get("due_date"))
        if not due or not (from_date <= due <= to_date):
            continue

        events.append(_event(
            "HOMEWORK",
            assignment.get("title") or "Homework",
            None,
            all_day=True,
            due_date=due.isoformat(),
            class_id=assignment.get("class_id"),
            class_name=_name_of(firestore_classes, assignment.get("class_id")),
            subject_id=assignment.get("subject_id"),
            subject_name=_name_of(firestore_subjects, assignment.get("subject_id")),
            teacher_id=assignment.get("teacher_id"),
            reference_id=assignment.get("id"),
        ))
    return events


# ---------------------------------------------------------------------------------------
# Assembling
# ---------------------------------------------------------------------------------------

def for_user(user, from_date: date, to_date: date,
             kinds: set[str] | None = None) -> dict:
    """
    Everything on this person's calendar in a date range.

    Role decides the sources, not a parameter: a student sees their class's periods and
    their own tuition, a teacher sees what they teach. Letting the caller ask for somebody
    else's calendar would make this an authorization decision taken in the router, which is
    where such decisions get forgotten.
    """
    _assert_range(from_date, to_date)

    role = getattr(user, "role", None)
    user_id = int(getattr(user, "id"))
    programs = set(getattr(user, "programs", None) or [Program.LMS.value])

    events: list[dict] = []

    if role == UserRole.STUDENT:
        enrollments = firestore_student_enrollments.query_documents(
            "student_id", "==", user_id
        )
        class_ids = {int(e["class_id"]) for e in enrollments if e.get("class_id") is not None}

        if class_ids:
            timetable = [
                t for t in firestore_timetable.list_all()
                if int(t.get("class_id", -1)) in class_ids
            ]
            events += _timetable_events(timetable, from_date, to_date, role)
            events += _meeting_events(
                [m for m in firestore_meetings.list_all()
                 if int(m.get("class_id", -1)) in class_ids],
                from_date, to_date,
            )
            events += _exam_events(
                [e for e in firestore_exams.list_all()
                 if int(e.get("class_id", -1)) in class_ids],
                from_date, to_date,
            )
            events += _homework_events(
                [h for h in firestore_homework.list_all()
                 if int(h.get("class_id", -1)) in class_ids],
                from_date, to_date,
            )

        if Program.TUITION.value in programs:
            mine = {
                str(e["id"]) for e in
                firestore_tuition_enrollments.query_documents("student_id", "==", user_id)
            }
            events += _tuition_events(
                [s for s in firestore_tuition_sessions.list_all()
                 if str(s.get("enrollment_id")) in mine],
                from_date, to_date,
            )

    elif role in (UserRole.TEACHER, UserRole.CLASS_TEACHER, UserRole.ADMIN):
        if role == UserRole.ADMIN:
            timetable = firestore_timetable.list_all()
            meetings = firestore_meetings.list_all()
            exams = firestore_exams.list_all()
            homework = firestore_homework.list_all()
        else:
            # What they teach, plus everything in a class they lead - a class teacher's
            # calendar has to show the periods they are answerable for, not only their own.
            led = permissions.led_class_ids(user)
            timetable = [
                t for t in firestore_timetable.list_all()
                if int(t.get("teacher_id", -1)) == user_id
                or int(t.get("class_id", -1)) in led
            ]
            meetings = [
                m for m in firestore_meetings.list_all()
                if int(m.get("teacher_id", -1)) == user_id
                or int(m.get("class_id", -1)) in led
            ]
            exams = [
                e for e in firestore_exams.list_all()
                if int(e.get("teacher_id", -1)) == user_id
                or int(e.get("class_id", -1)) in led
            ]
            homework = [
                h for h in firestore_homework.list_all()
                if int(h.get("teacher_id", -1)) == user_id
                or int(h.get("class_id", -1)) in led
            ]

        events += _timetable_events(timetable, from_date, to_date, role)
        events += _meeting_events(meetings, from_date, to_date)
        events += _exam_events(exams, from_date, to_date)
        events += _homework_events(homework, from_date, to_date)

        if Program.TUITION.value in programs or role == UserRole.ADMIN:
            mine = {
                str(e["id"]) for e in
                firestore_tuition_enrollments.query_documents("teacher_id", "==", user_id)
            } if role != UserRole.ADMIN else None
            events += _tuition_events(
                [s for s in firestore_tuition_sessions.list_all()
                 if mine is None or str(s.get("enrollment_id")) in mine],
                from_date, to_date,
            )

    elif role == UserRole.PARENT:
        # A parent's calendar is the union of their children's, each event tagged with whose
        # it is so a family with three children can tell them apart.
        from app.services import families as family_service

        for child in family_service.children_of(user_id):
            if not child.get("may_view_academics"):
                continue
            child_user = firestore_users.get_document(str(child["student_id"]))
            if not child_user:
                continue

            child_view = for_user(
                type("Child", (), {
                    "id": int(child["student_id"]),
                    "role": UserRole.STUDENT,
                    "programs": child_user.get("programs") or [Program.LMS.value],
                })(),
                from_date, to_date, kinds,
            )
            for event in child_view["events"]:
                event["student_id"] = int(child["student_id"])
                event["student_name"] = child.get("full_name")
                events.append(event)

    if kinds:
        events = [e for e in events if e["kind"] in kinds]

    # All-day items last within a day: a student scanning a morning wants the 09:00 period
    # before the homework due at no particular time.
    events.sort(key=lambda e: (str(e.get("start_at") or e.get("due_date") or ""),
                               e.get("all_day", False)))

    return {
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "events": events,
        "count": len(events),
        "counts_by_kind": _counts(events),
    }


def _counts(events: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        counts[event["kind"]] = counts.get(event["kind"], 0) + 1
    return counts


def group_by_day(payload: dict) -> list[dict]:
    """
    The same events bucketed into days, for a tile or month view.

    Every day in the range appears, including the empty ones. A month grid needs the blanks;
    making the client synthesise them from a sparse list is how the last week of a month ends
    up rendered in the wrong column.
    """
    from_date = date.fromisoformat(payload["from_date"])
    to_date = date.fromisoformat(payload["to_date"])

    buckets: dict[str, list[dict]] = {}
    for event in payload["events"]:
        key = (event.get("start_at") or event.get("due_date") or "")[:10]
        if key:
            buckets.setdefault(key, []).append(event)

    days = []
    cursor = from_date
    while cursor <= to_date:
        key = cursor.isoformat()
        items = buckets.get(key, [])
        days.append({
            "date": key,
            "weekday": DayOfWeek.from_date(cursor).value,
            "events": items,
            "count": len(items),
        })
        cursor += timedelta(days=1)
    return days
