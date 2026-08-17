"""
Timetable storage, conflict detection, and resolution against real calendar dates.

A timetable entry is a *recurring rule*, not an event: "Maths, 7A, Tuesdays 09:00-09:45,
from 1 Sep until 20 Dec". Two things follow from that, and they drive most of this module:

  * Times are wall-clock, stored as "HH:MM" strings and interpreted in SCHOOL_TIMEZONE.
    Assembly is at 08:00 regardless of what the clocks did last weekend, so storing an
    absolute instant would drift across a DST boundary.
  * Answering "what does 7A have today" means resolving the rules against a date. That
    arithmetic lives here rather than in each router, and certainly not in the client.

Conflicts are checked on write, because a timetable that double-books a teacher is not a
data problem to be reported later - it is a schedule nobody can actually follow.
"""

import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException

from app.core.config import settings
from app.core.enums import TEACHING_OR_ADMIN_VALUES, DayOfWeek
from app.core.firebase import (
    firestore_classes, firestore_student_enrollments, firestore_subjects,
    firestore_teacher_mappings, firestore_timetable, firestore_users,
    hydrate_timetable_entry, prefetch_references,
)

logger = logging.getLogger("timetable")

TIME_FORMAT = "%H:%M"


def school_timezone() -> ZoneInfo:
    """
    The zone timetable times are interpreted in.

    Falls back to UTC rather than raising: a typo in SCHOOL_TIMEZONE should make reminders
    land at the wrong hour and say so loudly in the log, not stop the server from booting.
    """
    name = settings.resolved_school_timezone
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        logger.error("SCHOOL_TIMEZONE '%s' is not a known IANA zone; falling back to UTC.", name)
        return ZoneInfo("UTC")


def now_local() -> datetime:
    """Current time in the school's zone, as an aware datetime."""
    return datetime.now(school_timezone())


def format_time(value: time | str) -> str:
    """Serializes a time to the 'HH:MM' form stored in Firestore."""
    if isinstance(value, str):
        return parse_time(value).strftime(TIME_FORMAT)
    return value.strftime(TIME_FORMAT)


def parse_time(value: time | str | None) -> time:
    """
    Reads a stored time back.

    Accepts 'HH:MM' and 'HH:MM:SS' so entries written by an older revision, or pasted in by
    hand, still load.
    """
    if isinstance(value, time):
        return value
    if not value:
        raise ValueError("Missing time value")

    text = str(value).strip()
    for fmt in (TIME_FORMAT, "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized time value '{value}'. Expected HH:MM.")


def _parse_date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _overlaps(start_a: time, end_a: time, start_b: time, end_b: time) -> bool:
    """True when two same-day periods share any minute. Touching edges do not overlap."""
    return start_a < end_b and start_b < end_a


def _ranges_overlap(from_a, to_a, from_b, to_b) -> bool:
    """
    True when two open-ended effective-date windows intersect.

    Without this, replacing a period mid-term - ending the old entry on the 14th and
    starting the new one on the 15th - would be rejected as a clash with itself.
    """
    if to_a is not None and from_b is not None and to_a < from_b:
        return False
    if to_b is not None and from_a is not None and to_b < from_a:
        return False
    return True


def entry_active_on(entry: dict, on_date: date) -> bool:
    """True when a stored entry's recurrence covers `on_date`."""
    if not entry.get("is_active", True):
        return False

    if entry.get("day_of_week") != DayOfWeek.from_date(on_date).value:
        return False

    effective_from = _parse_date(entry.get("effective_from"))
    effective_to = _parse_date(entry.get("effective_to"))
    if effective_from and on_date < effective_from:
        return False
    if effective_to and on_date > effective_to:
        return False
    return True


def _sort_key(entry: dict) -> tuple:
    """Orders entries the way a printed timetable reads: by weekday, then by start time."""
    try:
        day = DayOfWeek(entry.get("day_of_week")).iso_weekday
    except ValueError:
        day = 99
    try:
        start = parse_time(entry.get("start_time"))
    except ValueError:
        start = time(23, 59)
    return (day, start)


def sort_entries(entries: list[dict]) -> list[dict]:
    return sorted(entries, key=_sort_key)


def list_entries(
    class_id: int | None = None,
    teacher_id: int | None = None,
    subject_id: int | None = None,
    day_of_week: DayOfWeek | None = None,
    include_inactive: bool = False,
) -> list[dict]:
    """
    Reads timetable entries, filtered.

    The most selective available field drives the Firestore query and the rest is filtered
    in memory - the collection is one row per period per class, so it stays small enough
    that a second query would cost more than the filtering saves.
    """
    if class_id is not None:
        entries = firestore_timetable.query_documents("class_id", "==", class_id)
    elif teacher_id is not None:
        entries = firestore_timetable.query_documents("teacher_id", "==", teacher_id)
    elif subject_id is not None:
        entries = firestore_timetable.query_documents("subject_id", "==", subject_id)
    else:
        entries = firestore_timetable.list_all()

    if teacher_id is not None:
        entries = [e for e in entries if e.get("teacher_id") == teacher_id]
    if subject_id is not None:
        entries = [e for e in entries if e.get("subject_id") == subject_id]
    if class_id is not None:
        entries = [e for e in entries if e.get("class_id") == class_id]
    if day_of_week is not None:
        entries = [e for e in entries if e.get("day_of_week") == day_of_week.value]
    if not include_inactive:
        entries = [e for e in entries if e.get("is_active", True)]

    return sort_entries(entries)


def default_teacher_for(class_id: int, subject_id: int) -> int | None:
    """The teacher already mapped to this subject and class, if there is exactly one."""
    mappings = [
        m for m in firestore_teacher_mappings.query_documents("class_id", "==", class_id)
        if m.get("subject_id") == subject_id
    ]
    if len(mappings) == 1:
        return mappings[0].get("teacher_id")
    return None


def find_conflicts(candidate: dict, exclude_id: int | None = None) -> list[str]:
    """
    Returns human-readable descriptions of every clash a candidate entry would create.

    Two kinds matter: a class cannot be in two lessons at once, and a teacher cannot be in
    two rooms at once. Room double-booking is deliberately not checked - schools routinely
    leave `room` blank or reuse a label loosely, and rejecting on it would block real
    timetables for a cosmetic reason.
    """
    day = candidate["day_of_week"]
    start = parse_time(candidate["start_time"])
    end = parse_time(candidate["end_time"])
    from_date = _parse_date(candidate.get("effective_from"))
    to_date = _parse_date(candidate.get("effective_to"))

    conflicts: list[str] = []
    seen: set[int] = set()

    def scan(existing: list[dict], describe) -> None:
        for other in existing:
            other_id = other.get("id")
            if other_id == exclude_id or other_id in seen:
                continue
            if not other.get("is_active", True) or other.get("day_of_week") != day:
                continue
            try:
                other_start = parse_time(other.get("start_time"))
                other_end = parse_time(other.get("end_time"))
            except ValueError:
                continue
            if not _overlaps(start, end, other_start, other_end):
                continue
            if not _ranges_overlap(
                from_date, to_date,
                _parse_date(other.get("effective_from")), _parse_date(other.get("effective_to")),
            ):
                continue

            seen.add(other_id)
            conflicts.append(describe(other, other_start, other_end))

    class_entries = firestore_timetable.query_documents("class_id", "==", candidate["class_id"])
    scan(class_entries, lambda o, s, e: (
        f"the class already has "
        f"{_subject_name(o.get('subject_id'))} on {day.title()} "
        f"{s.strftime(TIME_FORMAT)}-{e.strftime(TIME_FORMAT)} (entry {o.get('id')})"
    ))

    teacher_id = candidate.get("teacher_id")
    if teacher_id is not None:
        teacher_entries = firestore_timetable.query_documents("teacher_id", "==", teacher_id)
        scan(teacher_entries, lambda o, s, e: (
            f"{_user_name(teacher_id)} already teaches "
            f"{_class_name(o.get('class_id'))} on {day.title()} "
            f"{s.strftime(TIME_FORMAT)}-{e.strftime(TIME_FORMAT)} (entry {o.get('id')})"
        ))

    return conflicts


def _subject_name(subject_id) -> str:
    doc = firestore_subjects.get_document(str(subject_id)) if subject_id is not None else None
    return (doc or {}).get("name") or f"subject {subject_id}"


def _class_name(class_id) -> str:
    doc = firestore_classes.get_document(str(class_id)) if class_id is not None else None
    return (doc or {}).get("name") or f"class {class_id}"


def _user_name(user_id) -> str:
    doc = firestore_users.get_document(str(user_id)) if user_id is not None else None
    return (doc or {}).get("full_name") or f"user {user_id}"


def build_entry_document(payload, *, resolve_teacher: bool = True) -> dict:
    """
    Validates a create payload against the referenced records and returns the document to
    store. Raises HTTPException with a message naming the specific bad reference.
    """
    if not firestore_classes.get_document(str(payload.class_id)):
        raise HTTPException(status_code=404, detail=f"ClassRoom {payload.class_id} not found")
    if not firestore_subjects.get_document(str(payload.subject_id)):
        raise HTTPException(status_code=404, detail=f"Subject {payload.subject_id} not found")

    teacher_id = payload.teacher_id
    if teacher_id is None and resolve_teacher:
        teacher_id = default_teacher_for(payload.class_id, payload.subject_id)

    if teacher_id is not None:
        teacher = firestore_users.get_document(str(teacher_id))
        if not teacher:
            raise HTTPException(status_code=404, detail=f"Teacher {teacher_id} not found")
        if teacher.get("role") not in TEACHING_OR_ADMIN_VALUES:
            raise HTTPException(
                status_code=400,
                detail=f"User {teacher_id} is not a teacher and cannot be assigned a period.",
            )

    return {
        "class_id": payload.class_id,
        "subject_id": payload.subject_id,
        "teacher_id": teacher_id,
        "day_of_week": payload.day_of_week.value,
        "start_time": format_time(payload.start_time),
        "end_time": format_time(payload.end_time),
        "room": payload.room,
        "period_label": payload.period_label,
        "effective_from": (payload.effective_from or date.today()).isoformat(),
        "effective_to": payload.effective_to.isoformat() if payload.effective_to else None,
        "is_active": payload.is_active,
        "created_at": datetime.utcnow().isoformat(),
    }


def create_entry(payload, allow_conflicts: bool = False) -> dict:
    """Persists one timetable entry, refusing a clash unless explicitly overridden."""
    document = build_entry_document(payload)

    if not allow_conflicts:
        conflicts = find_conflicts(document)
        if conflicts:
            raise HTTPException(
                status_code=409,
                detail=(
                    "This period clashes with the existing timetable: "
                    + "; ".join(conflicts)
                    + ". Fix the clash, or resend with ?allow_conflicts=true to schedule it anyway."
                ),
            )

    entry_id = firestore_timetable.get_next_numeric_id()
    firestore_timetable.add_document(str(entry_id), document)
    document["id"] = entry_id
    return document


def class_ids_for_student(student_id: int) -> list[int]:
    """Every class a student is enrolled in."""
    enrollments = firestore_student_enrollments.query_documents("student_id", "==", student_id)
    return [e["class_id"] for e in enrollments if e.get("class_id") is not None]


def entries_for_student(student_id: int, day_of_week: DayOfWeek | None = None) -> list[dict]:
    """The timetable a student should see, across every class they are enrolled in."""
    class_ids = class_ids_for_student(student_id)
    if not class_ids:
        return []

    entries: list[dict] = []
    seen: set[int] = set()
    for class_id in class_ids:
        for entry in list_entries(class_id=class_id, day_of_week=day_of_week):
            if entry["id"] not in seen:
                seen.add(entry["id"])
                entries.append(entry)
    return sort_entries(entries)


def resolve_on_date(entries: list[dict], on_date: date, reference: datetime | None = None) -> list[dict]:
    """
    Turns recurring entries into concrete periods for one calendar date.

    Each result carries the absolute start and end instants plus `starts_in_minutes` and
    `is_current`, so a client can render "next class in 12 minutes" without reimplementing
    weekday and timezone arithmetic.
    """
    tz = school_timezone()
    reference = reference or datetime.now(tz)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=tz)

    resolved = []
    for entry in entries:
        if not entry_active_on(entry, on_date):
            continue
        try:
            start = parse_time(entry.get("start_time"))
            end = parse_time(entry.get("end_time"))
        except ValueError:
            logger.warning("Timetable entry %s has an unreadable time; skipping.", entry.get("id"))
            continue

        starts_at = datetime.combine(on_date, start, tzinfo=tz)
        ends_at = datetime.combine(on_date, end, tzinfo=tz)

        resolved.append({
            "entry": hydrate_timetable_entry(entry),
            "on_date": on_date,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "starts_in_minutes": int((starts_at - reference).total_seconds() // 60),
            "is_current": starts_at <= reference < ends_at,
        })

    resolved.sort(key=lambda p: p["starts_at"])
    return resolved


def upcoming_periods(entries: list[dict], days_ahead: int = 7, limit: int = 20) -> list[dict]:
    """
    The next `limit` periods from now, looking up to `days_ahead` days forward.

    Scanning forward day by day rather than filtering a single week handles the ordinary
    case where "next class" is tomorrow morning, and the awkward one where a class only
    meets on Thursdays and today is Friday.
    """
    tz = school_timezone()
    now = datetime.now(tz)
    today = now.date()

    upcoming: list[dict] = []
    for offset in range(max(days_ahead, 0) + 1):
        for period in resolve_on_date(entries, today + timedelta(days=offset), reference=now):
            if period["ends_at"] > now:
                upcoming.append(period)
                if len(upcoming) >= limit:
                    return upcoming
    return upcoming


def hydrate_many(entries: list[dict]) -> list[dict]:
    """Hydrates a list of entries, warming the reference cache in one batched read first."""
    prefetch_references(
        entries,
        ("teacher_id", firestore_users),
        ("class_id", firestore_classes),
        ("subject_id", firestore_subjects),
    )
    return [hydrate_timetable_entry(entry) for entry in entries]
