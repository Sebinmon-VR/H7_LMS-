"""
Tuition scheduling: recurring slots, conflict detection, and session generation.

## Why conflict detection is stricter here than in the LMS

A school timetable clashes when two periods want the same teacher. A tuition timetable
clashes when two classes want the same teacher **or the same student**, and the second half
is the one that actually bites. A student takes maths with one teacher and physics with
another, and those two teachers have no way of knowing about each other - so if the system
does not check, nothing does, and the student finds out at 17:00 on a Tuesday.

So every write here asks the same question from both ends: is this person - either person -
already committed to a class that overlaps? A slot is a recurring rule, so "overlaps" means
the weekday matches, the wall-clock periods intersect, and the effective-date windows
intersect. All three have to hold; dropping the last one would refuse a perfectly ordinary
mid-term teacher change as a clash with the slot it replaces.

## Slots versus sessions

A slot is the rule. A session is one class on one date, generated from a slot by
`generate_sessions`, which is idempotent: the document id is derived from (slot, date), so
running the sweep twice updates the same class instead of booking the day twice. That
property is what makes it safe to call from a background sweep, from an admin button, and
from the moment a slot is created, all without coordination.

Generation only ever *adds*. A session that has been started, cancelled, or rescheduled by
hand is never overwritten by a later sweep - the schedule is a plan, and what happened to a
particular class outranks it.
"""

import logging
from datetime import date, datetime, timedelta

from fastapi import HTTPException

from app.core.enums import DayOfWeek, TuitionSessionStatus
from app.core.firebase import (
    firestore_subjects, firestore_tuition_enrollments, firestore_tuition_sessions,
    firestore_tuition_slots, firestore_users, hydrate_tuition_slot, prefetch_tuition,
)
from app.services.tuition.common import (
    add_minutes_to_time, date_ranges_overlap, format_time, instants_overlap, now_utc,
    parse_date, periods_overlap, program_timezone, session_start_at, slot_active_on,
    store_dt, to_utc, user_id_of,
)
from app.services.tuition.enrollments import ACTIVE_STATUSES, require_enrollment
from app.services.tuition.settings_store import tuition_settings

logger = logging.getLogger("tuition.scheduling")


# ---------------------------------------------------------------------------------------
# Reading slots
# ---------------------------------------------------------------------------------------

def require_slot(slot_id) -> dict:
    slot = firestore_tuition_slots.get_document(str(slot_id))
    if not slot:
        raise HTTPException(status_code=404, detail=f"Tuition slot {slot_id} not found")
    return slot


def slots_for_person(user_id: int, as_teacher: bool) -> list[dict]:
    field = "teacher_id" if as_teacher else "student_id"
    return firestore_tuition_slots.query_documents(field, "==", user_id)


def slots_for_enrollment(enrollment_id) -> list[dict]:
    return firestore_tuition_slots.query_documents("enrollment_id", "==", enrollment_id)


def sort_slots(slots: list[dict]) -> list[dict]:
    """Weekday then start time, so a timetable reads the way a week does."""
    order = {day.value: day.iso_weekday for day in DayOfWeek}

    def key(slot: dict):
        return (
            order.get(str(slot.get("day_of_week")), 99),
            str(slot.get("start_time") or "99:99"),
        )

    return sorted(slots, key=key)


def hydrate_many(slots: list[dict]) -> list[dict]:
    if not slots:
        return []
    prefetch_tuition(slots)
    return [hydrate_tuition_slot(slot) for slot in slots]


# ---------------------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------------------

def _describe(slot: dict, whose: str) -> str:
    """
    A conflict message a human can act on.

    Names the person, the subject and the time rather than returning ids. "Teacher Anil Nair
    already takes Physics with Priya on TUESDAY 17:00-18:00" tells an admin what to move; a
    bare "conflict with slot 1737..." sends them querying the database.
    """
    subject = firestore_subjects.get_document(str(slot.get("subject_id"))) or {}
    other = firestore_users.get_document(
        str(slot.get("student_id") if whose == "Teacher" else slot.get("teacher_id"))
    ) or {}
    return (
        f"{whose} is already booked: {subject.get('name', 'a subject')} with "
        f"{other.get('full_name', 'another participant')} on {slot.get('day_of_week')} "
        f"{slot.get('start_time')}-{slot.get('end_time')}"
    )


def slot_conflicts(candidate: dict, exclude_id=None) -> list[str]:
    """
    Every reason a proposed slot cannot be scheduled, from both participants' diaries.

    Returns descriptions rather than raising, so a caller can either refuse the write or -
    for an admin deliberately overriding - record them as warnings. Both people are checked
    in the same pass and the messages say which of them is busy, because "there is a
    conflict" without saying whose is the least useful possible answer.
    """
    gap = tuition_settings()["min_gap_minutes"]
    day = str(candidate.get("day_of_week"))
    start = candidate.get("start_time")
    end = candidate.get("end_time")
    valid_from = parse_date(candidate.get("effective_from"))
    valid_to = parse_date(candidate.get("effective_to"))

    problems: list[str] = []
    seen: set = set()

    for person_id, whose in (
        (candidate.get("teacher_id"), "Teacher"),
        (candidate.get("student_id"), "Student"),
    ):
        if person_id is None:
            continue
        for other in slots_for_person(person_id, as_teacher=(whose == "Teacher")):
            if exclude_id is not None and str(other.get("id")) == str(exclude_id):
                continue
            if str(other.get("id")) in seen:
                continue
            if not other.get("is_active", True):
                continue
            if str(other.get("day_of_week")) != day:
                continue
            if not date_ranges_overlap(
                valid_from, valid_to,
                parse_date(other.get("effective_from")), parse_date(other.get("effective_to")),
            ):
                continue
            if not periods_overlap(start, end, other.get("start_time"), other.get("end_time"), gap):
                continue

            seen.add(str(other.get("id")))
            problems.append(_describe(other, whose))

    return problems


def session_conflicts(student_id: int, teacher_id: int, start_at: datetime,
                      end_at: datetime, exclude_id=None) -> list[str]:
    """
    Conflicts for a one-off class placed at an absolute time.

    Ad-hoc and rescheduled classes do not go through the weekly rules, so they need their own
    check against the sessions already on the calendar. Only classes that are still going to
    happen are considered - a cancelled class occupies nobody's evening.
    """
    gap = tuition_settings()["min_gap_minutes"]
    problems: list[str] = []
    seen: set = set()

    for person_id, whose in ((teacher_id, "Teacher"), (student_id, "Student")):
        if person_id is None:
            continue
        field = "teacher_id" if whose == "Teacher" else "student_id"
        for other in firestore_tuition_sessions.query_documents(field, "==", person_id):
            if exclude_id is not None and str(other.get("id")) == str(exclude_id):
                continue
            if str(other.get("id")) in seen:
                continue
            if other.get("status") in {
                TuitionSessionStatus.CANCELLED.value,
                TuitionSessionStatus.COMPLETED.value,
                TuitionSessionStatus.NO_SHOW_TEACHER.value,
                TuitionSessionStatus.NO_SHOW_STUDENT.value,
            }:
                continue

            other_start = to_utc(other.get("scheduled_start_at"))
            other_end = to_utc(other.get("scheduled_end_at"))
            if not other_start or not other_end:
                continue
            if not instants_overlap(start_at, end_at, other_start, other_end, gap):
                continue

            seen.add(str(other.get("id")))
            subject = firestore_subjects.get_document(str(other.get("subject_id"))) or {}
            problems.append(
                f"{whose} already has {subject.get('name', 'a class')} at "
                f"{other_start.isoformat()} (session {other.get('id')})"
            )

    return problems


def free_slot_check(enrollment_id, day_of_week, start_time, duration_minutes,
                    effective_from=None, effective_to=None, exclude_id=None) -> list[str]:
    """
    Conflict check for a proposed slot, used by the admin's 'can I put it here?' endpoint.

    Exists so a scheduling UI can grey out impossible times *before* the admin commits to
    one, instead of letting them fill in a form and rejecting it at the end.
    """
    enrollment = require_enrollment(enrollment_id)
    start = format_time(start_time)
    return slot_conflicts(
        {
            "student_id": enrollment["student_id"],
            "teacher_id": enrollment["teacher_id"],
            "day_of_week": day_of_week.value if hasattr(day_of_week, "value") else str(day_of_week),
            "start_time": start,
            "end_time": add_minutes_to_time(start, duration_minutes),
            "effective_from": effective_from,
            "effective_to": effective_to,
        },
        exclude_id=exclude_id,
    )


# ---------------------------------------------------------------------------------------
# Writing slots
# ---------------------------------------------------------------------------------------

def build_slot_document(enrollment: dict, payload, actor) -> dict:
    """
    Assembles a slot from an enrollment plus the requested time.

    The participants are copied from the enrollment and never taken from the request. A
    caller cannot schedule a class for a student who is not on the arrangement, because the
    only place those ids come from is the arrangement itself.
    """
    config = tuition_settings()
    duration = (
        payload.duration_minutes
        or enrollment.get("default_duration_minutes")
        or config["default_session_minutes"]
    )
    start = format_time(payload.start_time)

    return {
        "enrollment_id": enrollment["id"],
        "student_id": enrollment["student_id"],
        "teacher_id": enrollment["teacher_id"],
        "subject_id": enrollment["subject_id"],
        "day_of_week": payload.day_of_week.value,
        "start_time": start,
        "duration_minutes": int(duration),
        "end_time": add_minutes_to_time(start, duration),
        "effective_from": payload.effective_from.isoformat() if payload.effective_from else None,
        "effective_to": payload.effective_to.isoformat() if payload.effective_to else None,
        "is_active": payload.is_active,
        "meeting_link": payload.meeting_link,
        "auto_create_meet": payload.auto_create_meet,
        "timezone": config["timezone"],
        "created_by": user_id_of(actor),
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": None,
    }


def create_slot(payload, actor, allow_conflicts: bool = False) -> dict:
    """
    Books a recurring class time.

    Conflicts refuse the write by default. `allow_conflicts` is an admin-only override for
    the case the system cannot know about - a student who genuinely does attend two things
    that overlap on paper because one of them is ending next week - and the conflicts are
    returned on the response either way, so an override is a decision somebody made rather
    than a silent one.
    """
    enrollment = require_enrollment(payload.enrollment_id)
    if enrollment.get("status") not in ACTIVE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Enrollment {enrollment['id']} is {enrollment.get('status')} and cannot "
                   f"take new class times.",
        )

    document = build_slot_document(enrollment, payload, actor)
    conflicts = slot_conflicts(document)
    if conflicts and not allow_conflicts:
        raise HTTPException(status_code=409, detail={"detail": "Scheduling conflict", "conflicts": conflicts})

    slot_id = firestore_tuition_slots.get_next_numeric_id()
    firestore_tuition_slots.add_document(str(slot_id), document)
    document["id"] = slot_id
    document["conflicts"] = conflicts

    # Generate the classes this rule implies straight away, so the student and teacher see
    # their new timetable immediately rather than after the next sweep.
    generated = generate_sessions(slot=document, actor=actor)
    document["sessions_generated"] = generated
    return document


def update_slot(slot: dict, payload, actor, allow_conflicts: bool = False) -> dict:
    """
    Moves or edits a recurring class time.

    Re-checks conflicts against the *merged* result, excluding the slot itself - otherwise
    every edit would clash with the version being edited. Future generated sessions are
    rebuilt so the change is visible on the calendar; classes already taught are left alone.
    """
    changed = payload.model_dump(exclude_unset=True)
    updates: dict = {}

    if "day_of_week" in changed and changed["day_of_week"] is not None:
        value = changed["day_of_week"]
        updates["day_of_week"] = value.value if hasattr(value, "value") else str(value)

    start = format_time(changed["start_time"]) if changed.get("start_time") else slot.get("start_time")
    duration = changed.get("duration_minutes") or slot.get("duration_minutes")
    if "start_time" in changed or "duration_minutes" in changed:
        updates["start_time"] = start
        updates["duration_minutes"] = int(duration)
        updates["end_time"] = add_minutes_to_time(start, duration)

    for field in ("effective_from", "effective_to"):
        if field in changed:
            value = parse_date(changed[field])
            updates[field] = value.isoformat() if value else None

    for field in ("is_active", "meeting_link", "auto_create_meet"):
        if field in changed:
            updates[field] = changed[field]

    if not updates:
        return {**slot, "conflicts": []}

    merged = {**slot, **updates}
    conflicts = slot_conflicts(merged, exclude_id=slot["id"])
    if conflicts and not allow_conflicts:
        raise HTTPException(status_code=409, detail={"detail": "Scheduling conflict", "conflicts": conflicts})

    updates["updated_at"] = datetime.utcnow().isoformat()
    firestore_tuition_slots.add_document(str(slot["id"]), updates)
    merged = {**slot, **updates}

    removed = drop_future_sessions(slot["id"])
    merged["conflicts"] = conflicts
    merged["sessions_removed"] = removed
    merged["sessions_generated"] = generate_sessions(slot=merged, actor=actor)
    return merged


def delete_slot(slot_id) -> dict[str, int]:
    """
    Removes a recurring class time and the classes it had already put on the calendar.

    Only *untouched future* sessions go. A class that has been started, taught, cancelled or
    rescheduled is a record of something that happened and is not the schedule's to delete.
    """
    removed = drop_future_sessions(slot_id)
    firestore_tuition_slots.delete_document(str(slot_id))
    return {"future_sessions_removed": removed}


def drop_future_sessions(slot_id) -> int:
    """Deletes still-untouched, still-to-come sessions generated from one slot."""
    moment = now_utc()
    removed = 0
    for session in firestore_tuition_sessions.query_documents("slot_id", "==", slot_id):
        if session.get("is_ad_hoc"):
            continue
        if session.get("status") != TuitionSessionStatus.SCHEDULED.value:
            continue
        if session.get("teacher_joined_at") or session.get("student_joined_at"):
            continue
        start = to_utc(session.get("scheduled_start_at"))
        if start and start > moment:
            firestore_tuition_sessions.delete_document(str(session["id"]))
            removed += 1
    return removed


# ---------------------------------------------------------------------------------------
# Session generation
# ---------------------------------------------------------------------------------------

def generated_session_id(slot_id, on_date: date) -> str:
    """
    The document id for a generated class.

    Derived from (slot, date) rather than allocated, which is what makes generation
    idempotent: the sweep, an admin's manual run, and the create-slot call can all race each
    other and the worst outcome is that the same class is written twice with identical
    content.
    """
    return f"slot{slot_id}_{on_date.isoformat()}"


def occurrences(slot: dict, from_date: date, to_date: date) -> list[date]:
    """Every date in a window on which a slot's rule fires."""
    try:
        weekday = DayOfWeek(str(slot.get("day_of_week"))).iso_weekday
    except ValueError:
        logger.warning("Slot %s has an unrecognized day '%s'.", slot.get("id"), slot.get("day_of_week"))
        return []

    # Step forward to the first matching weekday rather than walking every day in the range.
    cursor = from_date + timedelta(days=(weekday - from_date.isoweekday()) % 7)
    dates = []
    while cursor <= to_date:
        if slot_active_on(slot, cursor):
            dates.append(cursor)
        cursor += timedelta(days=7)
    return dates


def generate_sessions(slot: dict | None = None, actor=None, horizon_days: int | None = None,
                      from_date: date | None = None) -> int:
    """
    Materializes concrete classes from recurring slots, up to the configured horizon.

    Generating a bounded window rather than the whole term keeps the collection proportional
    to what anybody is actually going to look at, and means a slot edited in October does not
    have to rewrite classes in June. The sweep extends the horizon as time passes.

    Returns the number of classes created. Existing documents are left completely untouched -
    `create_document` is a claim, not a merge - so a class somebody has already interacted
    with can never be reset by a later run.
    """
    config = tuition_settings()
    horizon = horizon_days or config["session_horizon_days"]
    zone = program_timezone()
    start_date = from_date or datetime.now(zone).date()
    end_date = start_date + timedelta(days=horizon)

    slots = [slot] if slot else [
        s for s in firestore_tuition_slots.list_all() if s.get("is_active", True)
    ]

    # An enrollment that is paused or finished keeps its slots - that is the point of pausing
    # - but must stop putting classes on the calendar.
    enrollment_status: dict[str, str] = {}
    created = 0

    for entry in slots:
        enrollment_id = entry.get("enrollment_id")
        if enrollment_id is None:
            continue
        if str(enrollment_id) not in enrollment_status:
            record = firestore_tuition_enrollments.get_document(str(enrollment_id)) or {}
            enrollment_status[str(enrollment_id)] = str(record.get("status") or "")
        if enrollment_status[str(enrollment_id)] != "ACTIVE":
            continue

        duration = int(entry.get("duration_minutes") or config["default_session_minutes"])
        for on_date in occurrences(entry, start_date, end_date):
            starts = session_start_at(on_date, entry.get("start_time"), zone)
            document = {
                "enrollment_id": enrollment_id,
                "slot_id": entry.get("id"),
                "student_id": entry.get("student_id"),
                "teacher_id": entry.get("teacher_id"),
                "subject_id": entry.get("subject_id"),
                "session_date": on_date.isoformat(),
                "scheduled_start_at": store_dt(starts),
                "scheduled_end_at": store_dt(starts + timedelta(minutes=duration)),
                "duration_minutes": duration,
                "status": TuitionSessionStatus.SCHEDULED.value,
                "meeting_link": entry.get("meeting_link"),
                "meet_status": "MANUAL" if entry.get("meeting_link") else None,
                "is_ad_hoc": False,
                "created_by": user_id_of(actor) if actor else entry.get("created_by"),
                "created_at": datetime.utcnow().isoformat(),
            }
            if firestore_tuition_sessions.create_document(
                generated_session_id(entry.get("id"), on_date), document
            ):
                created += 1

    if created:
        logger.info("Generated %s tuition sessions through %s.", created, end_date)
    return created


def horizon_summary() -> dict:
    """What the generator would do right now, for the admin's scheduling screen."""
    config = tuition_settings()
    zone = program_timezone()
    today = datetime.now(zone).date()
    active_slots = [s for s in firestore_tuition_slots.list_all() if s.get("is_active", True)]
    return {
        "timezone": config["timezone"],
        "today": today.isoformat(),
        "horizon_days": config["session_horizon_days"],
        "horizon_end": (today + timedelta(days=config["session_horizon_days"])).isoformat(),
        "active_slots": len(active_slots),
    }


def all_conflicts() -> list[dict]:
    """
    Every clash currently sitting in the timetable.

    An admin who used the override, or whose teacher reassignment moved a slot into an
    occupied evening, needs somewhere to see the result. Pairs are de-duplicated so a clash
    between two slots is reported once rather than from each side.
    """
    slots = [s for s in firestore_tuition_slots.list_all() if s.get("is_active", True)]
    prefetch_tuition(slots)

    reported: set[tuple] = set()
    findings: list[dict] = []
    for entry in slots:
        for description in slot_conflicts(entry, exclude_id=entry.get("id")):
            key = tuple(sorted([str(entry.get("id")), description]))
            if key in reported:
                continue
            reported.add(key)
            findings.append({
                "slot_id": entry.get("id"),
                "enrollment_id": entry.get("enrollment_id"),
                "day_of_week": entry.get("day_of_week"),
                "start_time": entry.get("start_time"),
                "end_time": entry.get("end_time"),
                "conflict": description,
            })
    return findings
