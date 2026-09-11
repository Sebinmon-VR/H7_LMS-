"""
Tuition sessions: the class itself, its timing rules, and its attendance.

# The timing rules

This is the module the brief is most specific about, so it is worth stating the rule in
plain language before the code does it:

    A class is a fixed length, set by the administrator. If the **teacher** joins late, the
    student is still owed a full lesson, so the class runs on. If the **student** joins
    late, nothing moves - the teacher may finish at the scheduled time, and the minutes the
    student missed are the student's own.

Four stored instants and one derived one express that:

    scheduled_start_at ─┬─ teacher_joined_at ──► pushes the end out (capped)
                        └─ student_joined_at ──► records lateness, moves nothing
    scheduled_end_at   ──► effective_end_at: the earliest the teacher may stop

`effective_end_at` is recomputed on every join and is the single number the client should
count down to. It is stored rather than computed on read because it is financial data as
much as display data: an invoice queried three months later must produce the same answer
the participants saw at the time, and a formula re-evaluated against today's settings would
not.

## Two things this deliberately does not do

**It does not stop a teacher ending early.** `effective_end_at` is the teacher's *earliest
entitled* stop, not a lock. Connections drop and students leave; a teacher who cannot end a
class that is already over would simply close the tab, and the record would be worse. An
early finish is recorded - `ended_early`, `short_by_minutes` - and shows on the admin's
report, which is the honest way to surface it.

**It does not infer one party's attendance from the other's.** A teacher marks the student's
attendance; the student's own join time is recorded separately. When those two disagree it
is usually because something real happened, and a system that overwrote the teacher's
judgement with a timestamp would hide it.
"""

import logging
from datetime import datetime, timedelta

from fastapi import HTTPException

from app.core.config import settings as env_settings
from app.core.enums import (
    AttendanceStatus, CONDUCTED_SESSION_VALUES, TuitionSessionStatus,
)
from app.core.firebase import (
    firestore_subjects, firestore_tuition_sessions, firestore_users,
    hydrate_tuition_session, prefetch_tuition,
)
from app.core.google_meet import create_meeting as create_google_meet
from app.services.tuition.common import (
    is_admin, now_utc, parse_date, store_dt, to_utc, user_id_of,
)
from app.services.tuition.enrollments import require_enrollment
from app.services.tuition.scheduling import session_conflicts
from app.services.tuition.settings_store import tuition_settings

logger = logging.getLogger("tuition.sessions")


# ---------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------

def require_session(session_id) -> dict:
    record = firestore_tuition_sessions.get_document(str(session_id))
    if not record:
        raise HTTPException(status_code=404, detail=f"Tuition session {session_id} not found")
    return record


def may_view(session: dict, user) -> bool:
    return is_admin(user) or user_id_of(user) in {
        session.get("student_id"), session.get("teacher_id")
    }


def assert_may_view(session: dict, user) -> None:
    if not may_view(session, user):
        raise HTTPException(status_code=403, detail="This class is not yours to view.")


def assert_is_teacher_of(session: dict, user) -> None:
    """
    Guards the teacher-only actions: starting, ending, marking attendance.

    An admin passes, because an admin standing in for an absent teacher is a real situation
    and refusing it would leave the class unmarkable. Every such write records
    `attendance_marked_by`, so a record filled in by the office is distinguishable from one
    filled in by the teacher.
    """
    if is_admin(user):
        return
    if user_id_of(user) != session.get("teacher_id"):
        raise HTTPException(status_code=403, detail="Only the assigned teacher may do this.")


def list_for_person(user_id: int, as_teacher: bool) -> list[dict]:
    field = "teacher_id" if as_teacher else "student_id"
    return firestore_tuition_sessions.query_documents(field, "==", user_id)


def filter_sessions(records: list[dict], from_date=None, to_date=None,
                    status: str | None = None, subject_id: int | None = None,
                    enrollment_id=None) -> list[dict]:
    """
    Narrows a set of sessions in memory.

    Filtering here rather than in Firestore is a deliberate trade. Compound queries on date
    plus status plus subject would each need their own composite index, created by hand
    before the endpoint works at all in a fresh project; a person's sessions number in the
    hundreds, so the filter costs nothing and the feature works the moment it is deployed.
    """
    start = parse_date(from_date)
    end = parse_date(to_date)
    results = []
    for record in records:
        on_date = parse_date(record.get("session_date"))
        if start and (on_date is None or on_date < start):
            continue
        if end and (on_date is None or on_date > end):
            continue
        if status and record.get("status") != status:
            continue
        if subject_id is not None and record.get("subject_id") != subject_id:
            continue
        if enrollment_id is not None and str(record.get("enrollment_id")) != str(enrollment_id):
            continue
        results.append(record)
    return sort_sessions(results)


def sort_sessions(records: list[dict]) -> list[dict]:
    """Chronological. A timetable read out of order is not a timetable."""
    return sorted(records, key=lambda r: str(r.get("scheduled_start_at") or ""))


def hydrate_many(records: list[dict]) -> list[dict]:
    if not records:
        return []
    prefetch_tuition(records)
    return [hydrate_tuition_session(record) for record in records]


# ---------------------------------------------------------------------------------------
# The timing rules
# ---------------------------------------------------------------------------------------

def class_started_at(session: dict, config: dict | None = None) -> datetime | None:
    """
    When the class actually began - the moment student lateness is measured from.

    Not the same thing as the scheduled start, and conflating the two is unfair to the
    student in a way that shows up on their attendance record and their bill. Two modes,
    chosen by the administrator with `auto_start_class`:

      * **Teacher starts it** (default). The class begins when the teacher presses start.
        Until then there is nothing to be late for: a student who joins a class the teacher
        has not opened is early, not late, however long they have been waiting.
      * **Starts automatically.** The class opens on its timetabled slot whether or not the
        teacher has arrived, so lateness is measured from the timetable.

    In teacher-start mode the reference is `max(scheduled_start, started_at)`, never the raw
    start stamp. A teacher who opens the room five minutes early has not thereby made the
    student five minutes late for turning up on time.

    Returns None when the class has not started and is not due to - which is what tells
    `compute_timing` there is no lateness to calculate.
    """
    config = config or tuition_settings()
    scheduled_start = to_utc(session.get("scheduled_start_at"))

    if config["auto_start_class"]:
        return scheduled_start

    started = to_utc(session.get("started_at")) or to_utc(session.get("teacher_joined_at"))
    if started is None:
        return None
    return max(started, scheduled_start) if scheduled_start else started


def compute_timing(session: dict) -> dict:
    """
    Works out the end time and the two lateness figures from what has been recorded.

    The two lateness figures are measured against **different references**, and that
    asymmetry is the whole rule:

      * **The teacher** is late against the *timetable*. They promised to be there at 17:00,
        and being late is what earns the student extra time at the end.
      * **The student** is late against the *class actually starting*. You cannot be late for
        something that has not begun, so a student who joins before the teacher opens the
        class is not late at all - and one who joins ten minutes after a class that itself
        started twelve minutes late is ten minutes late, not twenty-two.

    Pure: takes the session as it stands and returns only the fields that change, so it can
    be reasoned about and tested without a database. Every write that touches a join time
    goes through here, which is what keeps the rule in one place instead of scattered across
    three endpoints that would drift apart.
    """
    config = tuition_settings()
    scheduled_start = to_utc(session.get("scheduled_start_at"))
    scheduled_end = to_utc(session.get("scheduled_end_at"))
    duration = int(session.get("duration_minutes") or config["default_session_minutes"])

    if scheduled_start is None:
        return {}
    if scheduled_end is None:
        scheduled_end = scheduled_start + timedelta(minutes=duration)

    teacher_joined = to_utc(session.get("teacher_joined_at"))
    student_joined = to_utc(session.get("student_joined_at"))
    began = class_started_at(session, config)

    teacher_late = _minutes_late(teacher_joined, scheduled_start)
    # None when the class never started: no reference, so no lateness. See `class_started_at`.
    student_late = _minutes_late(student_joined, began) if began else 0.0

    # A late teacher owes the student the lesson time they lost, so the class ends a full
    # duration after the teacher actually arrived - but never more than the cap past the
    # scheduled end, or one late teacher would push the student's next class off the evening
    # entirely. Measured from the teacher's arrival, not from the class opening, because in
    # auto-start mode an empty room is not a lesson.
    effective_end = scheduled_end
    extension = 0.0
    if teacher_joined and teacher_late > 0:
        earned_end = teacher_joined + timedelta(minutes=duration)
        cap = scheduled_end + timedelta(minutes=config["max_teacher_late_extension_minutes"])
        effective_end = min(earned_end, cap)
        extension = max((effective_end - scheduled_end).total_seconds() / 60.0, 0.0)

    return {
        "teacher_late_minutes": round(teacher_late, 2),
        "student_late_minutes": round(student_late, 2),
        "extension_minutes": round(extension, 2),
        "effective_end_at": store_dt(effective_end),
        # Stored so the figure can be explained afterwards without re-deriving it against
        # settings that may since have changed.
        "class_started_at": store_dt(began),
    }


def _minutes_late(joined: datetime | None, reference: datetime | None) -> float:
    """Minutes past a reference, never negative - arriving early is not -5 late."""
    if joined is None or reference is None:
        return 0.0
    return max((joined - reference).total_seconds() / 60.0, 0.0)


def timing_view(session: dict) -> dict:
    """
    The countdown a client needs, resolved against the clock right now.

    Returned alongside the session rather than left to the frontend to derive, because the
    two sides of this rule must not be able to disagree about when the class ends. The
    teacher's screen and the student's screen read the same number from the same place.
    """
    now = now_utc()
    scheduled_start = to_utc(session.get("scheduled_start_at"))
    effective_end = to_utc(session.get("effective_end_at")) or to_utc(session.get("scheduled_end_at"))

    remaining = None
    if effective_end:
        remaining = round((effective_end - now).total_seconds() / 60.0, 2)

    config = tuition_settings()
    began = class_started_at(session, config)
    # In auto-start mode the class opens on the timetable, so it has begun once the clock
    # passes it - whether or not the sweep has got round to stamping the status yet. Deriving
    # it here rather than trusting the stored status means a client is never told the class
    # has not started when it plainly has.
    has_started = bool(began and now >= began)

    return {
        "now": now.isoformat(),
        "starts_in_minutes": (
            round((scheduled_start - now).total_seconds() / 60.0, 2) if scheduled_start else None
        ),
        "minutes_remaining": remaining,
        "effective_end_at": store_dt(effective_end),
        "extension_minutes": session.get("extension_minutes", 0.0),
        "teacher_late_minutes": session.get("teacher_late_minutes", 0.0),
        "student_late_minutes": session.get("student_late_minutes", 0.0),
        # True once the teacher is entitled to stop. The one flag a teacher's UI needs.
        "may_end_now": bool(effective_end and now >= effective_end),
        "is_live": session.get("status") == TuitionSessionStatus.IN_PROGRESS.value,
        # --- Has the class actually begun? -------------------------------------------
        "auto_start": config["auto_start_class"],
        "class_started_at": store_dt(began),
        "class_has_started": has_started,
        # What a student's screen should say while they wait. A student who joins a class
        # the teacher has not opened is waiting, not late, and telling them otherwise is how
        # a fair rule reads as an unfair one.
        "waiting_for_teacher": bool(
            not has_started and not config["auto_start_class"]
            and scheduled_start and now >= scheduled_start
        ),
    }


# ---------------------------------------------------------------------------------------
# Attending the class
# ---------------------------------------------------------------------------------------

def teacher_join(session: dict, actor) -> dict:
    """
    Records the teacher arriving, which starts the class and fixes the extension.

    Idempotent on the join time: a teacher who refreshes the page does not reset their
    arrival to now, which would erase an extension the student is owed. The first arrival is
    the one that counts.
    """
    assert_is_teacher_of(session, actor)
    _assert_open(session)

    updates: dict = {}
    if not session.get("teacher_joined_at"):
        updates["teacher_joined_at"] = store_dt(now_utc())
        # In auto-start mode the class began on the timetable, so the teacher arriving does
        # not redefine when it started - it only records when they got there. Overwriting
        # `started_at` here would make a late teacher's arrival look like the class opening,
        # and a student who was waiting punctually would suddenly read as on time for a
        # class that had in fact been running without them.
        if not session.get("started_at"):
            updates["started_at"] = updates["teacher_joined_at"]

    if session.get("status") == TuitionSessionStatus.SCHEDULED.value:
        updates["status"] = TuitionSessionStatus.IN_PROGRESS.value

    merged = {**session, **updates}
    updates.update(compute_timing(merged))
    return _save(session, updates)


def student_join(session: dict, actor) -> dict:
    """
    Records the student arriving.

    Changes nothing about when the class ends - that is the rule, stated once in
    `compute_timing`. What it does set is the lateness figure the attendance default reads
    from, and that is measured against the moment the class actually began, not the
    timetable: a student who joins before the teacher has started is not late, and one who
    joins ten minutes into a class that itself began twelve minutes late is ten minutes
    late, not twenty-two.

    Joining before the class starts is allowed and is the normal case - the student is in
    the room waiting. `timing.waiting_for_teacher` is what their screen should read from.
    """
    if not is_admin(actor) and user_id_of(actor) != session.get("student_id"):
        raise HTTPException(status_code=403, detail="This class is not yours to join.")
    _assert_open(session)

    updates: dict = {}
    if not session.get("student_joined_at"):
        updates["student_joined_at"] = store_dt(now_utc())

    merged = {**session, **updates}
    updates.update(compute_timing(merged))
    return _save(session, updates)


def _assert_open(session: dict) -> None:
    if session.get("status") in {
        TuitionSessionStatus.CANCELLED.value,
        TuitionSessionStatus.COMPLETED.value,
    }:
        raise HTTPException(
            status_code=409,
            detail=f"This class is {session.get('status').lower()} and cannot be joined.",
        )


def end_session(session: dict, actor, topic: str | None = None,
                notes: str | None = None, recording_url: str | None = None) -> dict:
    """
    Closes the class.

    Records how long it actually ran and, if it finished before the teacher was entitled to
    stop, by how much. See the module docstring for why that is recorded rather than
    refused: the alternative is a teacher who closes the tab instead, and a class that stays
    IN_PROGRESS forever.

    Taught minutes are measured from the *later* of the two arrivals, because teaching does
    not begin until both people are in the room - which is also what makes an HOURLY fee
    plan bill for the lesson rather than for the wait.
    """
    assert_is_teacher_of(session, actor)
    if session.get("status") in {TuitionSessionStatus.CANCELLED.value,
                                 TuitionSessionStatus.COMPLETED.value}:
        raise HTTPException(status_code=409, detail="This class is already closed.")

    now = now_utc()
    effective_end = to_utc(session.get("effective_end_at")) or to_utc(session.get("scheduled_end_at"))
    teacher_joined = to_utc(session.get("teacher_joined_at"))
    student_joined = to_utc(session.get("student_joined_at"))

    arrivals = [t for t in (teacher_joined, student_joined) if t is not None]
    taught_from = max(arrivals) if len(arrivals) == 2 else (arrivals[0] if arrivals else None)
    taught = round((now - taught_from).total_seconds() / 60.0, 2) if taught_from else 0.0

    short_by = 0.0
    if effective_end and now < effective_end:
        short_by = round((effective_end - now).total_seconds() / 60.0, 2)

    updates = {
        "status": TuitionSessionStatus.COMPLETED.value,
        "ended_at": store_dt(now),
        "actual_duration_minutes": max(taught, 0.0),
        "ended_early": short_by > 0,
        "short_by_minutes": short_by,
    }
    if topic is not None:
        updates["topic"] = topic
    if notes is not None:
        updates["teacher_notes"] = notes
    if recording_url is not None:
        updates["recording_url"] = recording_url

    # A class nobody attended is a no-show, not a completed lesson, and the fee module reads
    # this distinction directly. Recorded here rather than left to the sweep because the
    # teacher pressing "end" is the most reliable moment to know it.
    if teacher_joined and not student_joined:
        updates["status"] = TuitionSessionStatus.NO_SHOW_STUDENT.value
        updates.setdefault("attendance_status", AttendanceStatus.ABSENT.value)

    return _save(session, updates)


def mark_attendance(session: dict, actor, status: AttendanceStatus,
                    remarks: str | None = None) -> dict:
    """
    Records whether the student attended.

    The teacher's call, and it overrides whatever the join timestamps imply: a student whose
    connection failed and who phoned in is present, however the log reads. Storing it on the
    session rather than in a separate collection is what makes that unambiguous - there is
    one row, and the teacher owns it.
    """
    assert_is_teacher_of(session, actor)
    value = status.value if hasattr(status, "value") else str(status)

    updates = {
        "attendance_status": value,
        "attendance_remarks": remarks,
        "attendance_marked_by": user_id_of(actor),
        "attendance_marked_at": store_dt(now_utc()),
    }
    # Marking attendance on a class still showing as scheduled is how a teacher records one
    # that happened without anybody pressing the buttons. Trust it and close the class.
    if session.get("status") in {TuitionSessionStatus.SCHEDULED.value,
                                 TuitionSessionStatus.IN_PROGRESS.value}:
        updates["status"] = (
            TuitionSessionStatus.NO_SHOW_STUDENT.value
            if value == AttendanceStatus.ABSENT.value
            else TuitionSessionStatus.COMPLETED.value
        )
        updates.setdefault("ended_at", store_dt(now_utc()))

    return _save(session, updates)


def suggested_attendance(session: dict) -> str:
    """
    What the join times imply, offered to the teacher as a default rather than applied.

    A convenience for the attendance screen: the common case is a teacher confirming what
    already happened, and pre-filling it removes the click without removing the decision.
    """
    config = tuition_settings()
    if not session.get("student_joined_at"):
        return AttendanceStatus.ABSENT.value
    if float(session.get("student_late_minutes") or 0) > config["student_late_grace_minutes"]:
        return AttendanceStatus.LATE.value
    return AttendanceStatus.PRESENT.value


# ---------------------------------------------------------------------------------------
# Scheduling changes
# ---------------------------------------------------------------------------------------

def cancel_session(session: dict, actor, reason: str | None = None) -> dict:
    """
    Calls a class off.

    Kept rather than deleted, and with the canceller recorded. Who cancelled matters: an
    admin's report needs to distinguish a class the student called off from one the teacher
    did, and a deleted row answers neither question.
    """
    if session.get("status") == TuitionSessionStatus.COMPLETED.value:
        raise HTTPException(status_code=409, detail="A completed class cannot be cancelled.")

    return _save(session, {
        "status": TuitionSessionStatus.CANCELLED.value,
        "cancelled_by": user_id_of(actor),
        "cancellation_reason": reason,
        "is_billable": False,
    })


def reschedule_session(session: dict, actor, new_start: datetime,
                       duration_minutes: int | None = None,
                       allow_conflicts: bool = False) -> dict:
    """
    Moves one class without touching the recurring slot behind it.

    The distinction matters: "we cannot do this Tuesday, let us do Wednesday" moves one
    class, while editing the slot would move every Tuesday from now on. Conflicts are
    re-checked at the new time from both diaries, because a class moved into an occupied
    evening is the exact failure the scheduling rules exist to prevent.
    """
    if session.get("status") in {TuitionSessionStatus.COMPLETED.value,
                                 TuitionSessionStatus.NO_SHOW_STUDENT.value}:
        raise HTTPException(status_code=409, detail="A class that has already happened cannot be moved.")

    duration = int(duration_minutes or session.get("duration_minutes")
                   or tuition_settings()["default_session_minutes"])
    starts = to_utc(new_start)
    ends = starts + timedelta(minutes=duration)

    conflicts = session_conflicts(
        session.get("student_id"), session.get("teacher_id"), starts, ends,
        exclude_id=session.get("id"),
    )
    if conflicts and not allow_conflicts:
        raise HTTPException(status_code=409, detail={"detail": "Scheduling conflict", "conflicts": conflicts})

    updated = _save(session, {
        "rescheduled_from": session.get("scheduled_start_at"),
        "scheduled_start_at": store_dt(starts),
        "scheduled_end_at": store_dt(ends),
        "session_date": starts.date().isoformat(),
        "duration_minutes": duration,
        "status": TuitionSessionStatus.SCHEDULED.value,
        # The rule is recomputed from scratch at the new time; a lateness figure carried over
        # from the old one would be nonsense.
        "teacher_joined_at": None,
        "student_joined_at": None,
        "effective_end_at": store_dt(ends),
        "teacher_late_minutes": 0.0,
        "student_late_minutes": 0.0,
        "extension_minutes": 0.0,
    })
    updated["conflicts"] = conflicts
    return updated


def create_ad_hoc(payload, actor, allow_conflicts: bool = False) -> dict:
    """
    Books a one-off extra class outside the weekly pattern.

    Revision classes before an exam, a catch-up for a cancelled slot. Conflict-checked like
    everything else, and flagged `is_ad_hoc` so a later edit to the recurring slot does not
    sweep it away.
    """
    enrollment = require_enrollment(payload.enrollment_id)
    config = tuition_settings()
    duration = int(payload.duration_minutes
                   or enrollment.get("default_duration_minutes")
                   or config["default_session_minutes"])
    starts = to_utc(payload.scheduled_start_at)
    ends = starts + timedelta(minutes=duration)

    conflicts = session_conflicts(
        enrollment["student_id"], enrollment["teacher_id"], starts, ends
    )
    if conflicts and not allow_conflicts:
        raise HTTPException(status_code=409, detail={"detail": "Scheduling conflict", "conflicts": conflicts})

    session_id = firestore_tuition_sessions.get_next_numeric_id()
    document = {
        "enrollment_id": enrollment["id"],
        "slot_id": None,
        "student_id": enrollment["student_id"],
        "teacher_id": enrollment["teacher_id"],
        "subject_id": enrollment["subject_id"],
        "session_date": starts.date().isoformat(),
        "scheduled_start_at": store_dt(starts),
        "scheduled_end_at": store_dt(ends),
        "effective_end_at": store_dt(ends),
        "duration_minutes": duration,
        "status": TuitionSessionStatus.SCHEDULED.value,
        "title": payload.title,
        "meeting_link": payload.meeting_link,
        "meet_status": "MANUAL" if payload.meeting_link else None,
        "is_ad_hoc": True,
        "created_by": user_id_of(actor),
        "created_at": datetime.utcnow().isoformat(),
    }
    firestore_tuition_sessions.add_document(str(session_id), document)
    document["id"] = session_id

    if payload.auto_create_meet and not payload.meeting_link:
        document = ensure_meeting_link(document, actor)

    document["conflicts"] = conflicts
    return document


# ---------------------------------------------------------------------------------------
# The meeting link
# ---------------------------------------------------------------------------------------

def ensure_meeting_link(session: dict, actor, force: bool = False) -> dict:
    """
    Gives a class somewhere to happen, preferring Google Meet.

    Meet is generated lazily - on demand rather than for every class the generator creates -
    because a month of generated sessions would otherwise mean a month of Calendar API calls
    for classes that may be rescheduled or cancelled before anybody opens them. The link is
    created the first time somebody actually needs it.

    A hand-entered link always wins and is never overwritten; the brief asks explicitly for
    other meeting providers to be usable, and a teacher who pastes a Zoom link has said what
    they want. Failure to reach Google is recorded on the session and returned, never raised:
    a class with no link is a problem to fix, not a reason to lose the class.
    """
    if session.get("meeting_link") and not force:
        return session

    teacher = firestore_users.get_document(str(session.get("teacher_id"))) or {}
    subject = firestore_subjects.get_document(str(session.get("subject_id"))) or {}
    student = firestore_users.get_document(str(session.get("student_id"))) or {}
    starts = to_utc(session.get("scheduled_start_at")) or now_utc()

    title = session.get("title") or (
        f"{subject.get('name', 'Tuition')} - {student.get('full_name', 'student')}"
    )

    created = create_google_meet(
        teacher_email=teacher.get("email") or "",
        title=title,
        scheduled_time=starts,
        duration_minutes=int(session.get("duration_minutes") or 60),
        description=f"One-to-one tuition class. Subject: {subject.get('name', '-')}.",
        attendee_emails=[e for e in (student.get("email"), teacher.get("email")) if e],
        auto_record=env_settings.ENABLE_MEET_AUTO_RECORDING,
    )

    updates = {
        "meeting_link": created["meeting_link"],
        "google_event_id": created["event_id"],
        "google_calendar_id": created["calendar_id"],
        "meet_status": "CREATED" if created["ok"] else "FAILED",
        "meet_error": created["error"],
        "meet_space_name": created["space_name"],
        "meet_meeting_code": created["meeting_code"],
    }
    if not created["ok"]:
        logger.warning("Meet link for tuition session %s failed: %s",
                       session.get("id"), created["error"])
    return _save(session, updates)


def set_meeting_link(session: dict, actor, link: str) -> dict:
    """Replaces the link with one supplied by hand - Zoom, Teams, a permanent Meet room."""
    return _save(session, {
        "meeting_link": link,
        "meet_status": "MANUAL",
        "meet_error": None,
    })


# ---------------------------------------------------------------------------------------
# The background sweep
# ---------------------------------------------------------------------------------------

def auto_start_due_sessions(reference: datetime | None = None) -> int:
    """
    Opens classes that are due, when the administrator has asked for automatic starts.

    Only moves the status; it never invents a `teacher_joined_at`. Who actually turned up is
    a fact about people, and a sweep marking a teacher present for a class they slept through
    would corrupt both the attendance record and the no-show handling that makes such a class
    non-billable.

    A no-op when `auto_start_class` is off, and correctness never depends on it having run:
    `class_started_at` derives the same answer from the timetable, so lateness is right even
    if the sweep is minutes behind.
    """
    config = tuition_settings()
    if not config["auto_start_class"]:
        return 0

    now = reference or now_utc()
    started = 0
    for session in firestore_tuition_sessions.query_documents(
        "status", "==", TuitionSessionStatus.SCHEDULED.value
    ):
        scheduled_start = to_utc(session.get("scheduled_start_at"))
        effective_end = to_utc(session.get("effective_end_at")) or to_utc(
            session.get("scheduled_end_at")
        )
        # Due, and not already over - a class whose whole window passed while the server was
        # down belongs to `close_stale_sessions`, not here.
        if not scheduled_start or now < scheduled_start:
            continue
        if effective_end and now > effective_end:
            continue

        _save(session, {
            "status": TuitionSessionStatus.IN_PROGRESS.value,
            "started_at": store_dt(scheduled_start),
            "auto_started": True,
        })
        started += 1

    if started:
        logger.info("Auto-started %s tuition class(es).", started)
    return started


def close_stale_sessions(reference: datetime | None = None) -> dict[str, int]:
    """
    Settles classes that nobody closed, so counts and invoices are not left waiting on them.

    Three cases, each of which has a different financial meaning and so gets its own status:

      * The teacher never arrived within the no-show window. NO_SHOW_TEACHER, not billable -
        the student did not get their lesson and should not pay for it.
      * The teacher arrived, the student never did. NO_SHOW_STUDENT, billable by default -
        the teacher's time was reserved and spent.
      * The class ran and nobody pressed 'end'. COMPLETED, with the recorded end taken as the
        entitled end rather than the moment the sweep happened to notice.

    Only classes whose window is fully past are touched, so a class running long is never
    closed underneath the people in it.
    """
    now = reference or now_utc()
    config = tuition_settings()
    grace = timedelta(minutes=config["teacher_no_show_minutes"])
    counts = {"teacher_no_show": 0, "student_no_show": 0, "auto_completed": 0}

    open_states = {TuitionSessionStatus.SCHEDULED.value, TuitionSessionStatus.IN_PROGRESS.value}
    for status_value in open_states:
        for session in firestore_tuition_sessions.query_documents("status", "==", status_value):
            scheduled_start = to_utc(session.get("scheduled_start_at"))
            scheduled_end = to_utc(session.get("scheduled_end_at"))
            effective_end = to_utc(session.get("effective_end_at")) or scheduled_end
            if not scheduled_start or not effective_end or now <= effective_end:
                continue

            teacher_joined = session.get("teacher_joined_at")
            student_joined = session.get("student_joined_at")

            if not teacher_joined and now > scheduled_start + grace:
                _save(session, {
                    "status": TuitionSessionStatus.NO_SHOW_TEACHER.value,
                    "ended_at": store_dt(effective_end),
                    "is_billable": False,
                    "attendance_status": AttendanceStatus.EXCUSED.value,
                    "attendance_remarks": "Teacher did not join; class not held.",
                })
                counts["teacher_no_show"] += 1
            elif teacher_joined and not student_joined:
                _save(session, {
                    "status": TuitionSessionStatus.NO_SHOW_STUDENT.value,
                    "ended_at": store_dt(effective_end),
                    "attendance_status": AttendanceStatus.ABSENT.value,
                })
                counts["student_no_show"] += 1
            elif teacher_joined and student_joined:
                joined = max(to_utc(teacher_joined), to_utc(student_joined))
                _save(session, {
                    "status": TuitionSessionStatus.COMPLETED.value,
                    "ended_at": store_dt(effective_end),
                    "actual_duration_minutes": round(
                        (effective_end - joined).total_seconds() / 60.0, 2
                    ),
                    "auto_closed": True,
                })
                counts["auto_completed"] += 1

    if any(counts.values()):
        logger.info("Tuition sweep closed sessions: %s", counts)
    return counts


def is_billable(session: dict) -> bool:
    """
    Whether a class counts towards the bill.

    An explicit `is_billable` set by an admin always wins - that is how a goodwill free class
    is recorded. Otherwise it follows the status, and the two statuses that count are the two
    where the teacher's time was actually spent.
    """
    override = session.get("is_billable")
    if override is not None:
        return bool(override)
    return session.get("status") in CONDUCTED_SESSION_VALUES


# ---------------------------------------------------------------------------------------

def _save(session: dict, updates: dict) -> dict:
    """Persists a partial update and returns the session as it now stands."""
    if not updates:
        return session
    updates["updated_at"] = datetime.utcnow().isoformat()
    firestore_tuition_sessions.add_document(str(session["id"]), updates)
    return {**session, **updates}


def present(session: dict, viewer, hydrated: bool = True) -> dict:
    """
    One session as an API response: references expanded, times localized, countdown attached.

    Every router returns sessions through here rather than assembling the response itself.
    That is what guarantees the teacher's view and the student's view of the same class carry
    the same `effective_end_at` and the same countdown - the one thing in this module the two
    sides must never be able to disagree about.
    """
    from app.core.firebase import hydrate_tuition_session
    from app.services.tuition.common import localize

    payload = hydrate_tuition_session(session) if hydrated else dict(session)
    payload["timing"] = timing_view(session)
    payload["suggested_attendance"] = suggested_attendance(session)
    return localize(payload, viewer)


def present_many(records: list[dict], viewer) -> list[dict]:
    """The list form. Prefetches references once so N sessions cost one batched read, not N."""
    if not records:
        return []
    prefetch_tuition(records)
    return [present(record, viewer) for record in records]
