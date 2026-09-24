"""
One standing live-class room per school class.

The school asked for the classroom model rather than the meeting model: a class has ONE
Google Meet link, the students join it in the morning and stay, and each subject teacher
joins that same room when their period comes round. Before this, every scheduled session
minted its own Calendar event and Meet link, so a student's day was eight links and a
teacher who was late re-sending one.

The room lives on the class record (`class_rooms.room_*`), because that is what it is a
property of. Sessions (`live_meetings`) keep existing - they are still the unit of "which
teacher, which subject, which period", still gate when a period counts as started, and still
own their recording - but a session scheduled while `class_room_mode` is on borrows the
class's link instead of generating one (`meeting_fields_for_room`).

Two decisions worth knowing before changing this:

* **Who owns the room.** A Meet conference is owned by whoever's calendar it was created
  on, and its recordings land in that account's Drive. Rooms default to the school's
  Workspace identity (`GOOGLE_IMPERSONATION_FALLBACK`) rather than the teacher who happened
  to schedule the first period, so a room does not vanish with a teacher who leaves and
  every period's recording lands in one place. An admin may put a room on a particular
  teacher's calendar instead.
* **Joining is gated by the timetable, not by a session.** A student sits in the room
  across periods, so "may I join?" is answered by whether any period of this class is on
  (or about to be, within the join window), or any scheduled session is live. Teachers and
  admins may always enter their own class's room - somebody has to open it.

Tuition is untouched: its one-to-one sessions keep their own links (`tuition_sessions`).
"""

import logging
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.core import google_meet
from app.core.config import settings
from app.core.enums import TEACHING_OR_ADMIN_VALUES, UserRole
from app.core.firebase import (
    firestore_class_room_attendance, firestore_class_room_events, firestore_classes,
    firestore_meetings, firestore_student_enrollments, firestore_subjects,
    firestore_teacher_mappings, firestore_users, require_document,
)
from app.services import live_classes, permissions
from app.services import timetable as timetable_service
from app.services.tuition.settings_store import lms_settings

logger = logging.getLogger("class_rooms")

ROOM_NONE = "NONE"
ROOM_CREATED = "CREATED"
ROOM_MANUAL = "MANUAL"
ROOM_FAILED = "FAILED"

PROVIDER_MEET = "GOOGLE_MEET"
PROVIDER_MANUAL = "MANUAL"

# `meet_status` on a session that uses its class's room rather than a link of its own.
MEET_STATUS_CLASS_ROOM = "CLASS_ROOM"

# Every room field on a class document, so clearing a room clears all of it.
ROOM_FIELDS = (
    "room_link", "room_provider", "room_status", "room_error",
    "room_event_id", "room_calendar_id",
    "room_owner_id", "room_owner_email",
    "room_space_name", "room_meeting_code",
    "room_auto_record", "room_recording_status", "room_recording_error",
    "room_guest_emails", "room_guest_error",
    "room_access_type", "room_access_error", "room_access_checked_at",
    "room_attendance_synced_at", "room_attendance_error",
    "room_created_at", "room_created_by", "room_updated_at",
)

HOST_ROLES = (UserRole.ADMIN, UserRole.TEACHER, UserRole.CLASS_TEACHER)

# How long before a class's first timetabled period of the day the maintenance sweep makes
# its room ready. Long enough that a teacher opening the day's first period finds it there
# and a student's dashboard shows it before the bell; short enough that a room is never made
# for a class that has no lessons that day.
ROOM_LEAD_MINUTES = 30

# A failed creation is retried by the sweep, but not on every five-minute pass: a
# misconfigured delegation would otherwise cost one Google call per class per pass.
ROOM_RETRY_MINUTES = 30


def _now() -> str:
    return datetime.utcnow().isoformat()


def _parse_stored(value) -> datetime | None:
    """A stored naive-UTC timestamp, or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


# ---------------------------------------------------------------------------------------
# The room log
#
# Google will not tell this app who is in a Meet (the participants API needs scopes the
# school's delegation does not grant), so the LMS keeps its own record of what it can see:
# every join it handed a link out for, every period a teacher opened or closed, and every
# room it made. That is what the admin's live board reads.
# ---------------------------------------------------------------------------------------

EVENT_JOINED_ROOM = "JOINED_ROOM"
EVENT_LEFT_ROOM = "LEFT_ROOM"
EVENT_JOINED_SESSION = "JOINED_SESSION"
EVENT_STARTED = "STARTED"
EVENT_ENDED = "ENDED"
EVENT_ROOM_CREATED = "ROOM_CREATED"
EVENT_ROOM_REPLACED = "ROOM_REPLACED"
EVENT_ROOM_LINK_SET = "ROOM_LINK_SET"
EVENT_ROOM_CLEARED = "ROOM_CLEARED"

JOIN_EVENTS = frozenset({EVENT_JOINED_ROOM, EVENT_JOINED_SESSION, EVENT_STARTED})
# The events that say where a person is: their latest one decides whether they count as in.
PRESENCE_EVENTS = JOIN_EVENTS | {EVENT_LEFT_ROOM}
TEACHING_ROLE_NAMES = frozenset({UserRole.TEACHER.value, UserRole.CLASS_TEACHER.value, UserRole.ADMIN.value})


def log_event(class_id, action: str, user=None, meeting_id=None, detail: str | None = None) -> dict:
    """Records one thing that happened in a class's room. Never raises: a log must not fail a join."""
    role = getattr(user, "role", None)
    event = {
        "class_id": int(class_id),
        "meeting_id": int(meeting_id) if meeting_id is not None else None,
        "user_id": int(user.id) if user is not None and getattr(user, "id", None) is not None else None,
        "user_name": getattr(user, "full_name", None) if user is not None else None,
        "role": getattr(role, "value", role) if role is not None else None,
        "action": action,
        "detail": detail,
        "at": _now(),
    }
    try:
        event_id = firestore_class_room_events.get_next_numeric_id()
        firestore_class_room_events.add_document(str(event_id), event)
        event["id"] = event_id
    except Exception as exc:  # pragma: no cover - never fail the action for its log line
        logger.warning("Room event for class %s not recorded: %s", class_id, exc)
    return event


def events_for_class(class_id, since: datetime | None = None, limit: int = 200) -> list[dict]:
    """A class's room log, newest first. `since` is naive UTC."""
    rows = firestore_class_room_events.query_documents("class_id", "==", int(class_id))
    if since is not None:
        floor = since.isoformat()
        rows = [r for r in rows if str(r.get("at") or "") >= floor]
    rows.sort(key=lambda r: str(r.get("at") or ""), reverse=True)
    return rows[:limit]


def presence_from(events: list[dict]) -> dict[int, dict]:
    """
    Who is in the room as the LMS knows it: each person's LATEST join-or-leave event today.

    `events` newest first. Somebody whose last word was a join is counted as in; a leave
    counts them out. Meet's own record (`sync_room_attendance`) is the authority when the
    school has authorised it - this is the live approximation.
    """
    latest: dict[int, dict] = {}
    for event in events:
        user_id = event.get("user_id")
        if user_id is None or event.get("action") not in PRESENCE_EVENTS:
            continue
        if user_id not in latest:
            latest[user_id] = event
    return latest


def _movements(events: list[dict], user_id: int, tz) -> list[tuple[datetime, bool]]:
    """One person's joins and leaves today, oldest first, as (moment in school time, is_join)."""
    moves = []
    for event in events:
        if event.get("user_id") != user_id or event.get("action") not in PRESENCE_EVENTS:
            continue
        at = _parse_stored(event.get("at"))
        if at is None:
            continue
        moves.append((at.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz), event["action"] in JOIN_EVENTS))
    moves.sort(key=lambda m: m[0])
    return moves


def annotate_periods(periods: list[dict], events: list[dict], now: datetime,
                     open_before: timedelta, grace: timedelta) -> list[dict]:
    """
    Stamps each period with how its teacher turned up, from the LMS log:

      teacher_joined_at   when the teacher was first in the room during the period (the
                          period's start if they were already sitting in it)
      teacher_left_at     when they next left, if they did
      waiting_minutes     how long the students sat without them: joined-at minus the
                          start, or the time so far for a running period nobody has come to
      teacher_status      UPCOMING, IN, LEFT, NOT_YET (running, nobody yet) or ABSENT

    Meet's own record refines nothing here on purpose: it names people by display name,
    and a teacher who never took the LMS link is exactly the case the office wants flagged.
    """
    tz = now.tzinfo
    cache: dict[int, list[tuple[datetime, bool]]] = {}

    for period in periods:
        teacher_id = (period.get("entry") or {}).get("teacher_id")
        start, end = period["starts_at"], period["ends_at"]
        joined_at = left_at = None

        if teacher_id is not None:
            moves = cache.setdefault(int(teacher_id), _movements(events, int(teacher_id), tz))
            inside = False
            for at, is_join in moves:
                if at < start - open_before:
                    inside = is_join          # state carried in from before the window
                    continue
                if at > end + grace:
                    break
                if is_join and joined_at is None:
                    joined_at = max(at, start)
                elif not is_join and joined_at is not None and left_at is None:
                    left_at = at
                inside = is_join
            if joined_at is None and inside and moves and moves[0][0] < start - open_before:
                joined_at = start             # already sitting in when the period began

        if now < start:
            status, waiting = "UPCOMING", None
        elif joined_at is not None:
            status = "LEFT" if left_at is not None else "IN"
            waiting = max(0.0, (joined_at - start).total_seconds() / 60.0)
        elif now < end + grace:
            status, waiting = "NOT_YET", (now - start).total_seconds() / 60.0
        else:
            status, waiting = "ABSENT", None

        period["teacher_joined_at"] = joined_at
        period["teacher_left_at"] = left_at
        period["teacher_status"] = status
        period["waiting_minutes"] = round(waiting, 1) if waiting is not None else None

    return periods


def class_room_mode() -> bool:
    """Whether school sessions share their class's room. An admin setting; see config."""
    return bool(lms_settings()["class_room_mode"])


def has_room(class_room: dict | None) -> bool:
    return bool((class_room or {}).get("room_link"))


def require_class(class_id) -> dict:
    return require_document(firestore_classes, class_id, "ClassRoom")


# ---------------------------------------------------------------------------------------
# Creating, replacing and clearing a room
# ---------------------------------------------------------------------------------------

def _owner_for(owner_id, actor) -> tuple[int | None, str]:
    """
    Whose calendar hosts the room. See the module docstring for why the school identity
    is the default rather than the scheduling teacher.
    """
    if owner_id is not None:
        owner = require_document(firestore_users, owner_id, "Teacher")
        if owner.get("role") not in TEACHING_OR_ADMIN_VALUES:
            raise HTTPException(
                status_code=400,
                detail=f"User {owner_id} is not a teacher and cannot host a class room.",
            )
        if not owner.get("email"):
            raise HTTPException(status_code=400, detail="That teacher has no email address.")
        return int(owner["id"]), owner["email"]

    fallback = (settings.GOOGLE_IMPERSONATION_FALLBACK or "").strip()
    if fallback:
        return None, fallback

    actor_id = getattr(actor, "id", None)
    actor_email = getattr(actor, "email", None) or ""
    return (int(actor_id) if actor_id is not None else None), actor_email


def teacher_emails_for_class(class_id) -> list[str]:
    """
    Every teacher who takes or leads the class, by login email.

    These are the people Meet must let straight into the room. A teacher on a gmail address
    is an outsider to a room organised by the school's Workspace identity, and an outsider
    who is not on the guest list has to ask to join - which is the complaint this exists to
    settle.
    """
    class_id = int(class_id)
    teacher_ids = {
        m["teacher_id"] for m in firestore_teacher_mappings.query_documents("class_id", "==", class_id)
        if m.get("teacher_id") is not None
    } | set(permissions.teachers_of_class(class_id))
    if not teacher_ids:
        return []
    teachers = firestore_users.get_documents(list(teacher_ids))
    return sorted({
        t["email"].strip().lower() for t in teachers.values()
        if t.get("email") and t.get("is_active", True)
    })


def room_guests(class_room: dict) -> list[str]:
    return [str(e).lower() for e in (class_room.get("room_guest_emails") or [])]


def sync_room_guests(class_room: dict, extra_emails=(), notify: bool = True) -> tuple[dict, str | None]:
    """
    Puts every teacher of the class (and `extra_emails`) on the room's Calendar guest list.

    Returns `(class, error)`. A no-op for a pasted link - the LMS does not own that event -
    and when everybody wanted is already a guest. Best-effort otherwise: a failure is
    recorded on the class (`room_guest_error`) and never fails the caller, because a teacher
    who has to knock once is a nuisance and a period that could not be scheduled is a hole.
    """
    if class_room.get("room_provider") != PROVIDER_MEET or not class_room.get("room_event_id"):
        return class_room, None

    wanted = set(teacher_emails_for_class(class_room["id"]))
    wanted |= {str(e).strip().lower() for e in extra_emails if e}
    owner = (class_room.get("room_owner_email") or "").strip().lower()
    wanted.discard(owner)
    wanted.discard("")

    current = set(room_guests(class_room))
    if wanted <= current:
        return class_room, None

    merged = sorted(wanted | current)
    result = google_meet.add_attendees(
        owner_email=class_room.get("room_owner_email") or "",
        event_id=class_room["room_event_id"],
        attendee_emails=sorted(wanted - current),
        calendar_id=class_room.get("room_calendar_id"),
        notify=notify,
    )
    if not result["ok"]:
        updates = {"room_guest_error": result["error"], "room_updated_at": _now()}
        firestore_classes.add_document(str(class_room["id"]), updates)
        return {**class_room, **updates}, result["error"]

    updates = {"room_guest_emails": merged, "room_guest_error": None, "room_updated_at": _now()}
    firestore_classes.add_document(str(class_room["id"]), updates)
    logger.info("Class %s room guest list now has %d teacher(s).", class_room.get("id"), len(merged))
    return {**class_room, **updates}, None


def wanted_access_type() -> str | None:
    value = (settings.MEET_ACCESS_TYPE or "").strip().upper()
    return value or None


def room_is_open(class_room: dict) -> bool:
    """Whether the room admits anyone with the link (or no access policy is wanted)."""
    wanted = wanted_access_type()
    if class_room.get("room_provider") != PROVIDER_MEET or not wanted:
        return True
    return (class_room.get("room_access_type") or "").upper() == wanted


def open_room_access(class_room: dict) -> tuple[dict, str | None]:
    """
    Sets the room's Meet space to the configured access type - OPEN, so anyone with the
    link joins without asking. Returns `(class, error)`; the error carries Google's reason
    and, when the delegation lacks the Meet scope, exactly what to authorise.
    """
    if class_room.get("room_provider") != PROVIDER_MEET or not class_room.get("room_link"):
        return class_room, "Only a Google Meet room made by the LMS can have its access set."
    if not wanted_access_type():
        return class_room, "MEET_ACCESS_TYPE is blank, so Google's default access stands."

    from app.core import meet_recordings

    result = meet_recordings.set_access_type(
        teacher_email=class_room.get("room_owner_email") or "",
        meeting_link=class_room["room_link"],
    )
    now = _now()
    if result["ok"]:
        updates = {
            "room_access_type": result["access_type"],
            "room_access_error": None,
            "room_access_checked_at": now,
            "room_space_name": class_room.get("room_space_name") or result["space_name"],
            "room_updated_at": now,
        }
        firestore_classes.add_document(str(class_room["id"]), updates)
        return {**class_room, **updates}, None

    updates = {"room_access_error": result["error"], "room_access_checked_at": now}
    firestore_classes.add_document(str(class_room["id"]), updates)
    return {**class_room, **updates}, result["error"]


def _room_event_start() -> datetime:
    """
    When the Calendar event behind the room is placed: the top of the next hour, school time.

    The event exists to carry the Meet link, not to be attended; its time is immaterial to
    the room, which stays usable as long as it is used. Placing it at a round hour keeps the
    owner's calendar tidy.
    """
    try:
        tz = ZoneInfo(settings.GOOGLE_CALENDAR_TIMEZONE or "UTC")
    except Exception:  # pragma: no cover - a bad zone must not stop room creation
        tz = ZoneInfo("UTC")
    now = datetime.now(tz)
    return (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


def _drop_calendar_event(class_room: dict) -> None:
    """Deletes the Calendar event behind a Meet room, best-effort. A manual room has none."""
    if class_room.get("room_provider") != PROVIDER_MEET or not class_room.get("room_event_id"):
        return
    try:
        google_meet.delete_meeting(
            teacher_email=class_room.get("room_owner_email") or "",
            event_id=class_room["room_event_id"],
            calendar_id=class_room.get("room_calendar_id"),
        )
    except Exception as exc:  # pragma: no cover - never fail a clear for its Calendar event
        logger.warning("Room event for class %s not deleted: %s", class_room.get("id"), exc)


def drop_calendar_event(class_room: dict) -> None:
    """Public form, for the class delete path."""
    _drop_calendar_event(class_room)


def create_room(class_room: dict, actor, owner_id=None, auto_record: bool = True,
                replace: bool = False) -> tuple[dict, str | None]:
    """
    Makes the class's Google Meet room.

    Returns `(class, error)`. `error` is None on success; on failure the class carries
    `room_status` FAILED and the reason, so the admin screen can show it and retry. Never
    raises for a Meet failure - the callers that create rooms lazily must not lose the
    session they were scheduling because Google said no.

    Idempotent unless `replace`: an existing room is returned untouched. Replacing deletes
    the old Calendar event first, so the previous link stops working.
    """
    if has_room(class_room) and not replace:
        return class_room, None
    if replace:
        _drop_calendar_event(class_room)

    owner_id_resolved, owner_email = _owner_for(owner_id, actor)
    name = class_room.get("name") or f"Class {class_room.get('id')}"

    # The class's teachers go on the guest list from the start, so Meet lets them straight
    # in rather than making them ask. The owner is the organiser and needs no invitation.
    guests = [e for e in teacher_emails_for_class(class_room["id"]) if e != owner_email.lower()]

    created = google_meet.create_meeting(
        teacher_email=owner_email,
        title=f"{name} - class room",
        scheduled_time=_room_event_start(),
        duration_minutes=60,
        description=(
            f"The standing live-class room for {name}. Students join here and stay; each "
            f"subject teacher joins at their timetabled period. You are on the guest list so "
            f"Meet lets you in directly. Managed by the LMS."
        ),
        attendee_emails=guests,
        auto_record=auto_record,
    )
    # create_meeting retries without guests when Google refuses them; its warning says so.
    guests_kept = bool(guests) and "were not invited" not in (created.get("error") or "")

    now = _now()
    actor_id = getattr(actor, "id", None)
    if created["ok"]:
        if not auto_record:
            recording_status = "NOT_REQUESTED"
        elif created["recording_armed"]:
            recording_status = "ARMED"
        else:
            recording_status = "ARM_FAILED"
        updates = {
            "room_link": created["meeting_link"],
            "room_provider": PROVIDER_MEET,
            "room_status": ROOM_CREATED,
            "room_error": created["error"],
            "room_event_id": created["event_id"],
            "room_calendar_id": created["calendar_id"],
            "room_owner_id": owner_id_resolved,
            "room_owner_email": owner_email,
            "room_space_name": created["space_name"],
            "room_meeting_code": created["meeting_code"],
            "room_auto_record": bool(auto_record),
            "room_recording_status": recording_status,
            "room_recording_error": created["recording_error"],
            "room_guest_emails": guests if guests_kept else [],
            "room_guest_error": (
                None if guests_kept or not guests else
                (created.get("error") or "Google would not add the teachers as guests.")
            ),
            # Who may walk in without asking. OPEN is the goal; anything else means Google
            # refused and the sweep keeps trying (see `ensure_rooms_for_today`).
            "room_access_type": created.get("access_type"),
            "room_access_error": created.get("access_error"),
            "room_access_checked_at": now,
            "room_created_at": now,
            "room_created_by": int(actor_id) if actor_id is not None else None,
            "room_updated_at": now,
        }
        error = None
        logger.info("Created class room for class %s (%s) owned by %s.",
                    class_room.get("id"), name, owner_email)
    else:
        error = created["error"] or "Google Meet did not return a link."
        updates = {
            **{field: None for field in ROOM_FIELDS},
            "room_status": ROOM_FAILED,
            "room_error": error,
            "room_owner_id": owner_id_resolved,
            "room_owner_email": owner_email,
            "room_auto_record": bool(auto_record),
            "room_updated_at": now,
        }
        logger.warning("Class room for class %s not created: %s", class_room.get("id"), error)

    firestore_classes.add_document(str(class_room["id"]), updates)
    if not error:
        log_event(
            class_room["id"], EVENT_ROOM_REPLACED if replace else EVENT_ROOM_CREATED, actor,
            detail=f"Google Meet room hosted by {owner_email}",
        )
    return {**class_room, **updates}, error


def set_manual_room(class_room: dict, link: str, actor) -> dict:
    """
    Records a link the school already has - Zoom, Teams, an existing Meet.

    Nothing is created or armed: the LMS does not own that conference, so it cannot record
    it or collect a recording afterwards. A Meet room the LMS made earlier is deleted first.
    """
    cleaned = (link or "").strip()
    if not cleaned.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Enter a full link, starting with https://")

    _drop_calendar_event(class_room)
    now = _now()
    actor_id = getattr(actor, "id", None)
    updates = {
        **{field: None for field in ROOM_FIELDS},
        "room_link": cleaned,
        "room_provider": PROVIDER_MANUAL,
        "room_status": ROOM_MANUAL,
        "room_auto_record": False,
        "room_recording_status": "NOT_REQUESTED",
        "room_created_at": now,
        "room_created_by": int(actor_id) if actor_id is not None else None,
        "room_updated_at": now,
    }
    firestore_classes.add_document(str(class_room["id"]), updates)
    logger.info("Class %s room set to a manual link by %s.", class_room.get("id"), actor_id)
    log_event(class_room["id"], EVENT_ROOM_LINK_SET, actor, detail=cleaned)
    return {**class_room, **updates}


def clear_room(class_room: dict, actor) -> dict:
    """Removes the room. Sessions already scheduled keep the link they were given."""
    _drop_calendar_event(class_room)
    updates = {
        **{field: None for field in ROOM_FIELDS},
        "room_status": ROOM_NONE,
        "room_updated_at": _now(),
    }
    firestore_classes.add_document(str(class_room["id"]), updates)
    logger.info("Class %s room cleared by %s.", class_room.get("id"), getattr(actor, "id", None))
    log_event(class_room["id"], EVENT_ROOM_CLEARED, actor)
    return {**class_room, **updates}


def ensure_room(class_room: dict, actor_id=None, actor_email: str | None = None) -> tuple[dict, str | None]:
    """
    The room, created on first need.

    Called when a session is scheduled or a teacher joins and the class has no room yet, so
    nobody has to visit the Classes screen before the first period can happen.
    """
    if has_room(class_room):
        return class_room, None
    actor = SimpleNamespace(id=actor_id, email=actor_email)
    return create_room(class_room, actor, auto_record=True)


def _system_owner(class_room: dict, periods: list[dict]) -> tuple[int | None, str | None]:
    """
    Who hosts a room the system creates on its own.

    The school's Workspace identity when one is configured - see the module docstring for
    why - else the class teacher, else whoever teaches the day's first period. Somebody has
    to own the Calendar event, and the class teacher is the person the school already holds
    answerable for the class.
    """
    fallback = (settings.GOOGLE_IMPERSONATION_FALLBACK or "").strip()
    if fallback:
        return None, fallback

    candidates = list(permissions.teachers_of_class(int(class_room["id"])))
    candidates += [
        p["entry"].get("teacher_id") for p in periods if p["entry"].get("teacher_id") is not None
    ]
    for teacher_id in candidates:
        teacher = firestore_users.get_document(str(teacher_id))
        if teacher and teacher.get("email") and teacher.get("is_active", True):
            return int(teacher["id"]), teacher["email"]
    return None, None


def ensure_rooms_for_today(reference: datetime | None = None,
                           class_ids: set[int] | None = None,
                           dry_run: bool = False) -> dict:
    """
    Makes sure every class with lessons today has its room before the first one starts.

    Run by the school maintenance sweep every few minutes. A class is due once the clock is
    within `ROOM_LEAD_MINUTES` of its first period and until its last period ends; a class
    that already has a room costs nothing, so in practice this fires once per class ever -
    and again after an admin removes a room. Nothing is done when the classroom model is
    switched off.

    `class_ids` narrows the pass (for a targeted run); `dry_run` reports what would be made.
    """
    result = {
        "class_room_mode": class_room_mode(),
        "dry_run": dry_run,
        "due": [],
        "created": [],
        "failed": [],
        "no_owner": [],
        "opened": [],
        "open_failed": [],
    }
    if not result["class_room_mode"]:
        return result

    tz = timetable_service.school_timezone()
    now = reference or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    today = now.date()
    lead = timedelta(minutes=ROOM_LEAD_MINUTES)
    retry_after = timedelta(minutes=ROOM_RETRY_MINUTES)
    now_utc = datetime.utcnow()

    for class_room in firestore_classes.list_all():
        class_id = int(class_room["id"])
        if class_ids is not None and class_id not in class_ids:
            continue
        if has_room(class_room):
            # A room Google would not open when it was made is tried again, throttled, so
            # rooms open themselves once the Meet scope is authorised.
            if not room_is_open(class_room) and not dry_run:
                last = _parse_stored(class_room.get("room_access_checked_at"))
                if not last or now_utc - last >= retry_after:
                    opened, error = open_room_access(class_room)
                    name = class_room.get("name") or f"Class {class_id}"
                    if error:
                        result["open_failed"].append({"class": name, "error": error})
                    else:
                        result["opened"].append(name)
            continue
        if class_room.get("room_status") == ROOM_FAILED:
            last_try = _parse_stored(class_room.get("room_updated_at"))
            if last_try and now_utc - last_try < retry_after:
                continue

        periods = timetable_service.resolve_on_date(
            timetable_service.list_entries(class_id=class_id), today, reference=now
        )
        if not periods:
            continue
        first_start = periods[0]["starts_at"]
        last_end = max(p["ends_at"] for p in periods)
        if not (first_start - lead <= now <= last_end):
            continue

        name = class_room.get("name") or f"Class {class_id}"
        result["due"].append(name)

        owner_id, owner_email = _system_owner(class_room, periods)
        if not owner_email:
            result["no_owner"].append(name)
            logger.warning("Class %s has lessons today but nobody to host its room.", name)
            continue
        if dry_run:
            result["created"].append({"class": name, "owner": owner_email, "first_period": first_start.isoformat()})
            continue

        updated, error = create_room(
            class_room, SimpleNamespace(id=owner_id, email=owner_email), auto_record=True
        )
        if error:
            result["failed"].append({"class": name, "error": error})
        else:
            result["created"].append({
                "class": name, "owner": owner_email, "room_link": updated.get("room_link"),
                "first_period": first_start.isoformat(),
            })

    if result["created"] and not dry_run:
        logger.info("Class rooms made ready for today: %s",
                    ", ".join(c["class"] for c in result["created"]))
    return result


def meeting_fields_for_room(class_room: dict) -> dict:
    """
    What a session stores when it uses the class's room instead of a link of its own.

    No Calendar event of its own - editing or cancelling the session must not touch the
    room's event - but the room's space and owner, which is what finds this period's
    recording afterwards (`app.services.recordings`).
    """
    return {
        "meeting_link": class_room.get("room_link"),
        "google_event_id": None,
        "google_calendar_id": None,
        "meet_status": MEET_STATUS_CLASS_ROOM,
        "meet_error": None,
        "meet_space_name": class_room.get("room_space_name"),
        "meet_meeting_code": class_room.get("room_meeting_code"),
        "meet_owner_email": class_room.get("room_owner_email"),
        "uses_class_room": True,
        "auto_record": bool(class_room.get("room_auto_record")),
        "recording_status": class_room.get("room_recording_status") or "NOT_REQUESTED",
        "recording_error": class_room.get("room_recording_error"),
        "meet_access_type": class_room.get("room_access_type"),
        "meet_access_error": class_room.get("room_access_error"),
    }


# ---------------------------------------------------------------------------------------
# Who may enter, and when
# ---------------------------------------------------------------------------------------

def visible_classes(user) -> list[dict]:
    """
    The classes whose room this user may see: enrolled in, teaches or leads, or all for an
    admin. A parent sees none here - they reach their children's classes elsewhere.
    """
    role = getattr(user, "role", None)
    user_id = int(getattr(user, "id"))

    if role == UserRole.ADMIN:
        rows = list(firestore_classes.list_all())
    elif role == UserRole.STUDENT:
        class_ids = {
            int(e["class_id"]) for e in
            firestore_student_enrollments.query_documents("student_id", "==", user_id)
            if e.get("class_id") is not None
        }
        rows = [c for c in firestore_classes.get_documents(list(class_ids)).values()]
    elif role in (UserRole.TEACHER, UserRole.CLASS_TEACHER):
        class_ids = {
            int(m["class_id"]) for m in
            firestore_teacher_mappings.query_documents("teacher_id", "==", user_id)
            if m.get("class_id") is not None
        } | set(permissions.led_class_ids(user))
        rows = [c for c in firestore_classes.get_documents(list(class_ids)).values()]
    else:
        rows = []

    from app.services.admissions import _natural_key
    rows.sort(key=lambda c: _natural_key(c.get("name") or ""))
    return rows


def assert_may_see(class_room: dict, user) -> None:
    """404 rather than 403 for a stranger: the room's existence is not theirs to learn."""
    wanted = int(class_room["id"])
    if any(int(c["id"]) == wanted for c in visible_classes(user)):
        return
    raise HTTPException(status_code=404, detail="Class not found")


def _fmt_time(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def access_for(class_room: dict, user, reference: datetime | None = None,
               entries: list[dict] | None = None, meetings: list[dict] | None = None,
               events: list[dict] | None = None, config: dict | None = None) -> dict:
    """
    The room as one person sees it right now: whether they may enter, why not, and the
    day's periods around it.

    A student joins ONCE for the day: the room is open to them from
    `join_open_minutes_before` ahead of the first period until `join_grace_minutes` after
    the last, breaks included - or while any scheduled session of the class is live. They
    sit in it; teachers come to them.

    A teacher may enter only around THEIR OWN periods - the same lead and grace either side
    of each - or a session of theirs that is live; the button is dead before and after. The
    class teacher of the class has the students' whole-day window, since the class is
    theirs to look in on. Admins may always enter.

    `entries`, `meetings` and `events` may be handed in by a caller that already read them
    for many classes at once (the live board); left out, they are read for this class alone.
    """
    config = config or lms_settings()
    tz = timetable_service.school_timezone()
    now = reference or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    today = now.date()

    if entries is None:
        entries = timetable_service.list_entries(class_id=int(class_room["id"]))
    periods = timetable_service.resolve_on_date(entries, today, reference=now)

    open_before = timedelta(minutes=int(config["join_open_minutes_before"]))
    grace = timedelta(minutes=int(config["join_grace_minutes"]))

    current = next((p for p in periods if p["is_current"]), None)
    upcoming = [p for p in periods if p["starts_at"] > now]
    next_period = upcoming[0] if upcoming else None

    # The day's window: one continuous stretch from just before the first period to just
    # after the last, so a student joins once and stays through the breaks.
    day_opens_at = (periods[0]["starts_at"] - open_before) if periods else None
    day_closes_at = (max(p["ends_at"] for p in periods) + grace) if periods else None
    day_open = bool(periods and day_opens_at <= now <= day_closes_at)

    # Where this person is, as far as the LMS saw today: their latest join or leave.
    if events is None:
        events = events_for_class(int(class_room["id"]), since=_school_day_start_utc(now))
    user_id = int(getattr(user, "id", -1))
    mine = presence_from(events).get(user_id)

    # How each period's teacher turned up, for the panels and the office's board.
    annotate_periods(periods, events, now, open_before, grace)

    # Scheduled sessions count too: an extra class on a Saturday has no timetable period,
    # and it still needs the room to open. Only today's and yesterday's rows are timed - a
    # class's whole history of sessions is not worth a clock computation per request.
    recent = {today.isoformat(), (today - timedelta(days=1)).isoformat()}
    live_meeting_ids = []
    my_live_meeting_ids = []
    if meetings is None:
        meetings = firestore_meetings.query_documents("class_id", "==", int(class_room["id"]))
    for meeting in meetings:
        if str(meeting.get("scheduled_time") or "")[:10] not in recent:
            continue
        timing = live_classes.timing_view(meeting, config)
        if timing["may_join"]:
            live_meeting_ids.append(int(meeting["id"]))
            if meeting.get("teacher_id") == user_id:
                my_live_meeting_ids.append(int(meeting["id"]))

    role = getattr(user, "role", None)
    is_host = role in HOST_ROLES
    is_admin = role == UserRole.ADMIN
    room_ready = has_room(class_room)
    mode = class_room_mode()

    # This person's own window on the room today.
    my_periods = [p for p in periods if (p.get("entry") or {}).get("teacher_id") == user_id] \
        if is_host and not is_admin else []
    my_current = next((p for p in my_periods if p["starts_at"] - open_before <= now <= p["ends_at"] + grace), None)
    my_next = next((p for p in my_periods if p["starts_at"] - open_before > now), None)
    leads_class = bool(is_host and not is_admin and permissions.is_class_teacher_of(user, class_room["id"]))

    if is_admin:
        window_open, window_opens_at, window_closes_at = True, None, None
    elif is_host and my_current is not None:
        window_open = True
        window_opens_at, window_closes_at = my_current["starts_at"] - open_before, my_current["ends_at"] + grace
    elif is_host and not leads_class:
        window_open = bool(my_live_meeting_ids)
        window_opens_at = (my_next["starts_at"] - open_before) if my_next else None
        window_closes_at = (my_next["ends_at"] + grace) if my_next else None
    else:
        # Students, and the class teacher looking in on their own class.
        window_open = day_open or bool(live_meeting_ids)
        window_opens_at, window_closes_at = day_opens_at, day_closes_at

    def _blocked_reason() -> str:
        if not periods:
            return "No class is timetabled for today."
        if is_host and not leads_class:
            if not my_periods:
                return "You have no period in this class today."
            if my_next is not None:
                subject = (my_next["entry"].get("subject") or {}).get("name") or "your next period"
                return (
                    f"Your {subject} period opens at {_fmt_time(window_opens_at)}, "
                    f"{int(config['join_open_minutes_before'])} minutes before it starts."
                )
            return "Your periods in this class are over for today."
        if now < day_opens_at:
            return (
                f"Your classroom opens at {_fmt_time(day_opens_at)}, "
                f"{int(config['join_open_minutes_before'])} minutes before the first class."
            )
        return "Classes are over for today. The room opens again with tomorrow's first period."

    reason = None
    if not room_ready:
        may_join = bool(is_host and mode and window_open)
        if not may_join:
            if not mode:
                reason = (
                    "This class has no room, and shared class rooms are switched off."
                    if is_host else
                    "This class does not have a room yet. Ask the office to set one up."
                )
            elif is_host and not window_open:
                reason = _blocked_reason()
            elif not periods:
                reason = "No class is timetabled for today."
            elif now < periods[0]["starts_at"] - timedelta(minutes=ROOM_LEAD_MINUTES):
                reason = (
                    f"Your classroom opens about {ROOM_LEAD_MINUTES} minutes before the first "
                    f"class of the day, at {_fmt_time(periods[0]['starts_at'] - timedelta(minutes=ROOM_LEAD_MINUTES))}."
                )
            else:
                # Due but not there: the sweep has not run yet, or Google refused it.
                reason = "Your classroom is being set up. Try again in a few minutes."
    else:
        may_join = window_open
        if not may_join:
            reason = _blocked_reason()

    return {
        "class_id": int(class_room["id"]),
        "class_name": class_room.get("name") or f"Class {class_room.get('id')}",
        "class_code": class_room.get("code"),
        "has_room": room_ready,
        "room_provider": class_room.get("room_provider"),
        "room_status": class_room.get("room_status") or (ROOM_NONE if not room_ready else None),
        "room_error": class_room.get("room_error"),
        "room_recording_status": class_room.get("room_recording_status"),
        "class_room_mode": mode,
        "is_host": is_host,
        "is_admin": is_admin,
        "now": now,
        "may_join": may_join,
        "join_blocked_reason": reason,
        "current_period": current,
        "next_period": next_period,
        "periods_today": periods,
        "day_opens_at": day_opens_at,
        "day_closes_at": day_closes_at,
        # This person's own window: the current or next stretch they may enter for. A
        # teacher's is around their period; a student's is the day; an admin has none.
        "window_opens_at": window_opens_at,
        "window_closes_at": window_closes_at,
        "my_current_period": my_current,
        "my_next_period": my_next,
        "leads_class": leads_class,
        "live_meeting_ids": live_meeting_ids,
        # This person's own last movement today, so their screen can say "you joined at
        # 9:02" and offer to record their leaving.
        "my_last_action": mine.get("action") if mine else None,
        "my_last_at": _parse_stored(mine.get("at")) if mine else None,
        "in_room": bool(mine and mine.get("action") in JOIN_EVENTS),
        # Handed over only when they may enter, the same rule the session join applies.
        "room_link": class_room.get("room_link") if (may_join and room_ready) else None,
    }


def presence_for_class(class_room: dict, reference: datetime | None = None) -> dict:
    """
    Who is in the room right now, by name - for the teacher standing in front of it and
    for the office. From the LMS log: everyone enrolled, split into those whose last word
    today was a join and those who left or never came, each with the time.
    """
    tz = timetable_service.school_timezone()
    now = reference or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    class_id = int(class_room["id"])

    events = events_for_class(class_id, since=_school_day_start_utc(now), limit=2000)
    latest = presence_from(events)

    student_ids = sorted({
        int(e["student_id"]) for e in firestore_student_enrollments.query_documents("class_id", "==", class_id)
        if e.get("student_id") is not None
    })
    users = firestore_users.get_documents(student_ids)

    def _row(uid: int, name: str, event: dict | None) -> dict:
        at = _parse_stored(event.get("at")) if event else None
        return {
            "user_id": uid,
            "name": name,
            "in_room": bool(event and event.get("action") in JOIN_EVENTS),
            "last_action": event.get("action") if event else None,
            "last_at": at,
        }

    students = []
    for uid in student_ids:
        doc = users.get(uid) or users.get(str(uid)) or {}
        if doc.get("is_active") is False:
            continue
        students.append(_row(uid, doc.get("full_name") or f"Student {uid}", latest.get(uid)))
    students.sort(key=lambda r: (not r["in_room"], r["name"].lower()))

    teachers = [
        _row(uid, event.get("user_name") or f"User {uid}", event)
        for uid, event in latest.items()
        if event.get("role") in TEACHING_ROLE_NAMES and event.get("action") in JOIN_EVENTS
    ]
    teachers.sort(key=lambda r: r["name"].lower())

    return {
        "class_id": class_id,
        "class_name": class_room.get("name") or f"Class {class_id}",
        "now": now,
        "enrolled_count": len(students),
        "in_count": sum(1 for s in students if s["in_room"]),
        "students": students,
        "teachers_in": teachers,
        "meet_attendance_synced_at": _parse_stored(class_room.get("room_attendance_synced_at")),
    }


def join_room(class_room: dict, user) -> dict:
    """
    Enter the class's room, creating it on the way if a host is first in and it does not
    exist yet. 409 with the reason when a student may not enter; 502 when the room could
    not be created.
    """
    access = access_for(class_room, user)

    if not access["has_room"]:
        if access["is_host"] and access["class_room_mode"]:
            class_room, error = ensure_room(
                class_room, actor_id=getattr(user, "id", None),
                actor_email=getattr(user, "email", None),
            )
            if not has_room(class_room):
                raise HTTPException(
                    status_code=502,
                    detail=error or "The class room could not be created.",
                )
            access = access_for(class_room, user)
        else:
            raise HTTPException(status_code=409, detail=access["join_blocked_reason"])

    if not access["may_join"]:
        raise HTTPException(status_code=409, detail=access["join_blocked_reason"])

    # A teacher who reaches the room through the LMS is put on its guest list if they are
    # not there yet - added after the room was made, or mapped since - so Meet stops asking
    # them to join from the next time on. Silent: an invitation email for a room they are
    # already entering would only confuse.
    if access["is_host"] and getattr(user, "email", None) \
            and getattr(user, "role", None) != UserRole.ADMIN \
            and user.email.strip().lower() not in room_guests(class_room):
        class_room, _ = sync_room_guests(class_room, extra_emails=[user.email], notify=False)

    current = access["current_period"]
    log_event(
        class_room["id"], EVENT_JOINED_ROOM, user,
        detail=(
            f"during {(current['entry'].get('subject') or {}).get('name') or 'a period'}"
            if current else None
        ),
    )

    return {
        "class_id": access["class_id"],
        "class_name": access["class_name"],
        "room_link": class_room.get("room_link"),
        "access": {**access, "room_link": class_room.get("room_link")},
    }


def leave_room(class_room: dict, user) -> dict:
    """
    Records that this person has left the room.

    The LMS cannot see a Meet tab close, so leaving is something the person tells it - the
    "Leave the class" button - and the log carries it as a LEFT_ROOM line with the time.
    Meet's own record, when the school has authorised it, is what confirms it.
    """
    access = access_for(class_room, user)
    current = access["current_period"]
    log_event(
        class_room["id"], EVENT_LEFT_ROOM, user,
        detail=(
            f"during {(current['entry'].get('subject') or {}).get('name') or 'a period'}"
            if current else None
        ),
    )
    return access_for(class_room, user)


# ---------------------------------------------------------------------------------------
# What Google Meet itself saw: join and leave times per participant
#
# The LMS records a join when it hands a link out and a leave when somebody says so. Meet
# knows the truth - who was in the call, from when to when - and gives it up through the
# conference records API once the school authorises the read scope. The sync below copies
# that into `class_room_attendance`, keeps trying quietly until the scope arrives, and says
# on the class (`room_attendance_error`) why it cannot yet.
# ---------------------------------------------------------------------------------------

ATTENDANCE_SYNC_MINUTES = 15
_meet_read_backoff_until = 0.0


def _roster_by_name(class_id: int) -> dict[str, dict]:
    """The class's students and teachers keyed by lower-cased full name, for matching."""
    people: dict[str, dict] = {}
    student_ids = [
        e.get("student_id") for e in firestore_student_enrollments.query_documents("class_id", "==", class_id)
        if e.get("student_id") is not None
    ]
    teacher_ids = [
        m.get("teacher_id") for m in firestore_teacher_mappings.query_documents("class_id", "==", class_id)
        if m.get("teacher_id") is not None
    ] + list(permissions.teachers_of_class(class_id))
    for doc in firestore_users.get_documents(student_ids + teacher_ids).values():
        name = str(doc.get("full_name") or "").strip().lower()
        if name:
            people.setdefault(name, {"id": int(doc["id"]), "name": doc.get("full_name"), "role": doc.get("role")})
    return people


def attendance_for_class(class_id, on_date) -> list[dict]:
    """Meet's record for one class on one day, earliest joiner first."""
    key = on_date.isoformat() if hasattr(on_date, "isoformat") else str(on_date)
    rows = [
        r for r in firestore_class_room_attendance.query_documents("class_id", "==", int(class_id))
        if str(r.get("date")) == key
    ]
    rows.sort(key=lambda r: (str(r.get("first_joined_at") or ""), str(r.get("display_name") or "")))
    return rows


def sync_room_attendance(class_room: dict, on_date=None, force: bool = False) -> tuple[list[dict], str | None]:
    """
    Copies Meet's participant sessions for the room into `class_room_attendance`.

    Throttled to once every `ATTENDANCE_SYNC_MINUTES` per class unless forced, and backed
    off for half an hour after Meet refuses the scope, so a school that has not authorised
    it costs one refused token exchange every thirty minutes rather than one per class per
    sweep. Returns `(rows for the day, error)`.
    """
    global _meet_read_backoff_until

    from app.core import meet_recordings

    class_id = int(class_room["id"])
    tz = timetable_service.school_timezone()
    day = on_date or datetime.now(tz).date()

    if class_room.get("room_provider") != PROVIDER_MEET or not (
        class_room.get("room_space_name") or class_room.get("room_meeting_code")
    ):
        return attendance_for_class(class_id, day), None

    now_utc = datetime.utcnow()
    if not force:
        last = _parse_stored(class_room.get("room_attendance_synced_at"))
        if last and now_utc - last < timedelta(minutes=ATTENDANCE_SYNC_MINUTES):
            return attendance_for_class(class_id, day), None
        if time.monotonic() < _meet_read_backoff_until:
            return attendance_for_class(class_id, day), class_room.get("room_attendance_error")

    day_start_local = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    since_utc = day_start_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    day_end_local = day_start_local + timedelta(days=1)

    listed = meet_recordings.list_participant_sessions(
        teacher_email=class_room.get("room_owner_email") or "",
        space_name=class_room.get("room_space_name"),
        meeting_code=class_room.get("room_meeting_code"),
        since=since_utc,
    )
    if not listed["ok"]:
        _meet_read_backoff_until = time.monotonic() + 30 * 60
        firestore_classes.add_document(str(class_id), {
            "room_attendance_error": listed["error"],
            "room_attendance_synced_at": now_utc.isoformat(),
        })
        return attendance_for_class(class_id, day), listed["error"]

    roster = _roster_by_name(class_id)
    synced = now_utc.isoformat()
    for participant in listed["participants"]:
        started = _parse_stored(participant.get("conference_start_time"))
        if started is None:
            continue
        started_local = started.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
        if not (day_start_local <= started_local < day_end_local):
            continue

        sessions = [
            {"joined_at": s.get("start_time"), "left_at": s.get("end_time")}
            for s in participant.get("sessions") or []
        ] or [{
            "joined_at": participant.get("earliest_start_time"),
            "left_at": participant.get("latest_end_time"),
        }]
        joins = [_parse_stored(s["joined_at"]) for s in sessions if s.get("joined_at")]
        leaves = [_parse_stored(s["left_at"]) for s in sessions if s.get("left_at")]
        minutes = 0.0
        for s in sessions:
            a, b = _parse_stored(s.get("joined_at")), _parse_stored(s.get("left_at"))
            if a and b and b > a:
                minutes += (b - a).total_seconds() / 60.0

        matched = roster.get(str(participant.get("display_name") or "").strip().lower())
        conference_id = str(participant.get("conference_record") or "").rsplit("/", 1)[-1]
        participant_id = str(participant.get("participant") or "").rsplit("/", 1)[-1]
        doc_id = f"{class_id}_{conference_id}_{participant_id}"

        firestore_class_room_attendance.add_document(doc_id, {
            "class_id": class_id,
            "date": day.isoformat(),
            "conference_record": participant.get("conference_record"),
            "conference_start_at": participant.get("conference_start_time"),
            "conference_end_at": participant.get("conference_end_time"),
            "participant": participant.get("participant"),
            "display_name": participant.get("display_name"),
            "user_kind": participant.get("user_kind"),
            "matched_user_id": matched["id"] if matched else None,
            "matched_user_name": matched["name"] if matched else None,
            "matched_role": matched["role"] if matched else None,
            "sessions": sessions,
            "first_joined_at": min(joins).isoformat() if joins else None,
            "last_left_at": max(leaves).isoformat() if leaves else None,
            # No end time on the latest session means they are still in the call.
            "still_in": any(not s.get("left_at") for s in sessions),
            "minutes": round(minutes, 1),
            "synced_at": synced,
        })

    firestore_classes.add_document(str(class_id), {
        "room_attendance_error": None, "room_attendance_synced_at": synced,
    })
    return attendance_for_class(class_id, day), None


def sync_attendance_for_today() -> dict:
    """The sweep's pass: every class whose first period has begun today, throttled."""
    result = {"synced": [], "failed": []}
    tz = timetable_service.school_timezone()
    now = datetime.now(tz)
    for class_room in firestore_classes.list_all():
        if class_room.get("room_provider") != PROVIDER_MEET or not class_room.get("room_link"):
            continue
        periods = timetable_service.resolve_on_date(
            timetable_service.list_entries(class_id=int(class_room["id"])), now.date(), reference=now
        )
        if not periods or now < periods[0]["starts_at"]:
            continue
        rows, error = sync_room_attendance(class_room)
        name = class_room.get("name") or f"Class {class_room.get('id')}"
        if error:
            result["failed"].append({"class": name, "error": error})
            if time.monotonic() < _meet_read_backoff_until:
                break  # the scope is the problem; every other class would fail the same way
        else:
            result["synced"].append(name)
    return result


# ---------------------------------------------------------------------------------------
# The admin's live board
# ---------------------------------------------------------------------------------------

def _school_day_start_utc(now_local: datetime) -> datetime:
    """Midnight of the school day, as the naive UTC the event log is stamped in."""
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def live_board(admin) -> list[dict]:
    """
    Every class as the office sees it right now: the room, the current and next period, who
    has come in today, whether a teacher is in, and any scheduled session that is live.

    Classes in session sort first. Everything here is computed on request from the
    timetable, the sessions and the room log; nothing is stored.
    """
    config = lms_settings()
    tz = timetable_service.school_timezone()
    now = datetime.now(tz)
    day_start_utc = _school_day_start_utc(now)
    open_before = timedelta(minutes=int(config["join_open_minutes_before"]))
    today_key = now.date().isoformat()

    # Three collection reads for the whole board rather than three per class: this is
    # polled every half minute, and each round trip to the database costs the same whether
    # it returns one class's rows or every class's.
    from app.core.firebase import firestore_timetable

    entries_by_class: dict[int, list[dict]] = {}
    for entry in firestore_timetable.list_all():
        if entry.get("class_id") is not None and entry.get("is_active", True):
            entries_by_class.setdefault(int(entry["class_id"]), []).append(entry)
    for bucket in entries_by_class.values():
        bucket.sort(key=lambda e: (str(e.get("day_of_week")), str(e.get("start_time"))))

    enrolled_by_class: dict[int, set] = {}
    for enrollment in firestore_student_enrollments.list_all():
        if enrollment.get("class_id") is not None and enrollment.get("student_id") is not None:
            enrolled_by_class.setdefault(int(enrollment["class_id"]), set()).add(enrollment["student_id"])

    meetings_by_class: dict[int, list[dict]] = {}
    for meeting in firestore_meetings.list_all():
        if meeting.get("class_id") is None:
            continue
        # Only what the clock could still care about: yesterday's overrun and today's.
        if str(meeting.get("scheduled_time") or "")[:10] < (now.date() - timedelta(days=1)).isoformat():
            continue
        meetings_by_class.setdefault(int(meeting["class_id"]), []).append(meeting)

    rows = []
    for class_room in visible_classes(admin):
        class_id = int(class_room["id"])
        events = events_for_class(class_id, since=day_start_utc, limit=500)
        access = access_for(
            class_room, admin, reference=now,
            entries=entries_by_class.get(class_id, []),
            meetings=meetings_by_class.get(class_id, []),
            events=events,
            config=config,
        )

        enrolled_count = len(enrolled_by_class.get(class_id, set()))

        # Who is in right now, by the LMS's reckoning: their last word today was a join.
        presence = presence_from(events)
        students_in_now = sorted(
            (e.get("user_name") or f"User {uid}") for uid, e in presence.items()
            if e.get("action") in JOIN_EVENTS and e.get("role") == UserRole.STUDENT.value
        )
        teachers_in_now = sorted(
            (e.get("user_name") or f"User {uid}") for uid, e in presence.items()
            if e.get("action") in JOIN_EVENTS and e.get("role") in TEACHING_ROLE_NAMES
        )
        students_joined = {
            e.get("user_id") for e in events
            if e.get("action") in JOIN_EVENTS and e.get("role") == UserRole.STUDENT.value
            and e.get("user_id") is not None
        }
        teachers_joined = []
        seen_teachers = set()
        for e in events:
            if e.get("action") in JOIN_EVENTS and e.get("role") in TEACHING_ROLE_NAMES \
                    and e.get("user_id") not in seen_teachers:
                seen_teachers.add(e.get("user_id"))
                teachers_joined.append(e.get("user_name") or f"User {e.get('user_id')}")

        # A teacher counts as present when they came in since the current period opened.
        teacher_present = False
        current = access["current_period"]
        if current is not None:
            opened_utc = (current["starts_at"] - open_before).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
            teacher_present = any(
                e.get("action") in JOIN_EVENTS and e.get("role") in TEACHING_ROLE_NAMES
                and (_parse_stored(e.get("at")) or datetime.min) >= opened_utc
                for e in events
            )

        # Today's scheduled sessions with their clocks; a live one opens the room whatever
        # the timetable says.
        live_sessions = []
        for meeting in meetings_by_class.get(class_id, []):
            if str(meeting.get("scheduled_time") or "")[:10] != today_key:
                continue
            timing = live_classes.timing_view(meeting, config)
            if timing["is_closed"] or timing["is_expired"]:
                continue
            if not (timing["is_live"] or timing["join_window_open"]):
                continue
            subject = firestore_subjects.get_document(str(meeting.get("subject_id"))) or {}
            teacher = firestore_users.get_document(str(meeting.get("teacher_id"))) or {}
            live_sessions.append({
                "meeting_id": int(meeting["id"]),
                "title": meeting.get("title"),
                "subject_name": subject.get("name"),
                "teacher_name": teacher.get("full_name"),
                "timing": timing,
            })

        rows.append({
            **access,
            "enrolled_count": enrolled_count,
            "students_joined_today": len(students_joined),
            "teachers_joined_today": teachers_joined,
            "teacher_present": teacher_present or bool(teachers_in_now),
            "students_in_now": students_in_now,
            "teachers_in_now": teachers_in_now,
            "live_sessions": live_sessions,
            "last_event": events[0] if events else None,
            "is_live": bool(current is not None or any(s["timing"]["is_live"] for s in live_sessions)),
            "room_attendance_synced_at": _parse_stored(class_room.get("room_attendance_synced_at")),
            "room_attendance_error": class_room.get("room_attendance_error"),
        })

    rows.sort(key=lambda r: (
        not r["is_live"],
        r["next_period"] is None,
        str((r["next_period"] or {}).get("starts_at") or ""),
        r["class_name"],
    ))
    return rows
