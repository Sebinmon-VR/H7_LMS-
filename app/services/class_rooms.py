"""
One standing live-class room per school class.

The school asked for the classroom model rather than the meeting model: a class has ONE
live-class room, the students join it in the morning and stay, and each subject teacher
joins that same room when their period comes round. Before this, every scheduled session
minted its own link, so a student's day was eight links and a teacher who was late
re-sending one.

The room is an Azure Communication Services room joined inside the LMS at /call/class/<id>
(see app/core/acs.py and app/services/calls.py). It replaced a standing Google Meet when the
school's Google Workspace was retired; the stored fields kept their names (`room_link`,
`meet_status` on sessions) so every screen that opened a link kept working.

The room lives on the class record (`class_rooms.room_*`), because that is what it is a
property of. Sessions (`live_meetings`) keep existing - they are still the unit of "which
teacher, which subject, which period", still gate when a period counts as started, and still
own their recording - but a session scheduled while `class_room_mode` is on borrows the
class's link instead of generating one (`meeting_fields_for_room`).

Two decisions worth knowing before changing this:

* **The LMS is the doorman.** An ACS room admits only the identities added to it. Nobody is
  added ahead of time; the call page asks the LMS to let a person in, the rules below decide,
  and only then are they put on the room's list with their role. So the timetable windows
  are enforced by the call itself, not just by whether a button was shown.
* **Joining is gated by the timetable, not by a session.** A student sits in the room
  across periods, so "may I join?" is answered by whether any period of this class is on
  (or about to be, within the join window), or any scheduled session is live. Teachers and
  admins may always enter their own class's room - somebody has to open it.

Tuition is untouched: its one-to-one sessions keep their own links (`tuition_sessions`).
"""

import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.core import acs
from app.core.config import settings
from app.core.enums import UserRole
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

PROVIDER_ACS = acs.PROVIDER
PROVIDER_MANUAL = "MANUAL"
# Rooms made before the move off Google. Their links died with the Workspace, so a class
# still carrying one is treated as having no room and gets an ACS room on next need.
PROVIDER_LEGACY_MEET = "GOOGLE_MEET"

# `meet_status` on a session that uses its class's room rather than a link of its own.
MEET_STATUS_CLASS_ROOM = "CLASS_ROOM"

# Every room field on a class document, so clearing a room clears all of it.
ROOM_FIELDS = (
    "room_link", "room_provider", "room_status", "room_error",
    "room_acs_room_id", "room_acs_valid_until",
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
# misconfigured resource would otherwise cost one ACS call per class per pass.
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
# The LMS keeps its own record of who is in each room: the call page reports every connect
# and every hang-up (app/services/calls.py), a pasted external link logs a join when it is
# handed out, and every period a teacher opened or closed and every room made is here too.
# That is what the admin's live board and the attendance view read.
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
    counts them out. For an LMS room the joins and leaves come from the call itself, so this
    is exact; for a pasted external link it is only what people told the LMS.
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

    A teacher who never came in through the LMS is exactly the case the office wants flagged.
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
    room = class_room or {}
    return bool(room.get("room_link")) and room.get("room_provider") != PROVIDER_LEGACY_MEET


def require_class(class_id) -> dict:
    return require_document(firestore_classes, class_id, "ClassRoom")


# ---------------------------------------------------------------------------------------
# Creating, replacing and clearing a room
# ---------------------------------------------------------------------------------------

def teacher_emails_for_class(class_id) -> list[str]:
    """Every teacher who takes or leads the class, by login email."""
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
    Kept for its callers; there is no guest list to keep any more.

    A Meet room let in its organiser and invited guests and made everybody else knock, so the
    LMS maintained a guest list of teachers. An ACS room admits exactly the people the LMS
    adds, and it adds each person at the moment it lets them in (`app.services.calls`), so
    there is nothing to synchronise ahead of time.
    """
    return class_room, None


def room_is_open(class_room: dict) -> bool:
    """Every LMS-made room admits whoever the LMS lets in; there is no access type to set."""
    return True


def open_room_access(class_room: dict) -> tuple[dict, str | None]:
    """Nothing to do: see `room_is_open`."""
    return class_room, None


def _drop_room(class_room: dict) -> None:
    """Deletes the ACS room behind a class, best-effort. A manual room has none."""
    if class_room.get("room_provider") == PROVIDER_ACS and class_room.get("room_acs_room_id"):
        acs.delete_room(class_room["room_acs_room_id"])


def drop_calendar_event(class_room: dict) -> None:
    """Public form, for the class delete path. (Named for the Meet era's Calendar event.)"""
    _drop_room(class_room)


def create_room(class_room: dict, actor, owner_id=None, auto_record: bool = True,
                replace: bool = False) -> tuple[dict, str | None]:
    """
    Makes the class's live-class room: an Azure Communication Services room, joined inside
    the LMS at /call/class/<id>.

    Returns `(class, error)`. `error` is None on success; on failure the class carries
    `room_status` FAILED and the reason, so the admin screen can show it and retry. Never
    raises for an ACS failure - the callers that create rooms lazily must not lose the
    session they were scheduling because the service said no.

    Idempotent unless `replace`: an existing room is returned untouched. Replacing deletes
    the old ACS room first. `owner_id` is accepted for older callers and ignored - an ACS room
    has no owner whose calendar or Drive it lives in.
    """
    if has_room(class_room) and not replace:
        return class_room, None
    if replace:
        _drop_room(class_room)

    name = class_room.get("name") or f"Class {class_room.get('id')}"
    now = _now()
    actor_id = getattr(actor, "id", None)
    recording_on = bool(auto_record and settings.ENABLE_AUTO_RECORDING)

    try:
        room = acs.create_room()
    except acs.AcsError as exc:
        error = str(exc)
        updates = {
            **{field: None for field in ROOM_FIELDS},
            "room_status": ROOM_FAILED,
            "room_error": error,
            "room_auto_record": bool(auto_record),
            "room_updated_at": now,
        }
        firestore_classes.add_document(str(class_room["id"]), updates)
        logger.warning("Class room for class %s not created: %s", class_room.get("id"), error)
        return {**class_room, **updates}, error

    updates = {
        **{field: None for field in ROOM_FIELDS},
        "room_link": acs.call_link(acs.KIND_CLASS, class_room["id"]),
        "room_provider": PROVIDER_ACS,
        "room_status": ROOM_CREATED,
        "room_error": None,
        "room_acs_room_id": room["room_id"],
        "room_acs_valid_until": _naive_utc(room["valid_until"]).isoformat(),
        "room_auto_record": bool(auto_record),
        "room_recording_status": "ARMED" if recording_on else "NOT_REQUESTED",
        "room_created_at": now,
        "room_created_by": int(actor_id) if actor_id is not None else None,
        "room_updated_at": now,
    }
    firestore_classes.add_document(str(class_room["id"]), updates)
    logger.info("Created class room for class %s (%s).", class_room.get("id"), name)
    log_event(
        class_room["id"], EVENT_ROOM_REPLACED if replace else EVENT_ROOM_CREATED, actor,
        detail="Live-class room created",
    )
    return {**class_room, **updates}, None


def _naive_utc(moment: datetime) -> datetime:
    from datetime import timezone

    return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo else moment


def ensure_acs_room(class_room: dict) -> tuple[dict, str]:
    """
    The class's ACS room id, valid for a while yet: extended when it nears its end, and
    replaced by a new room when the old one has gone. Raises acs.AcsError.
    """
    room_id = class_room.get("room_acs_room_id")
    if room_id:
        until = _parse_stored(class_room.get("room_acs_valid_until"))
        renew_by = datetime.utcnow() + timedelta(days=settings.ACS_ROOM_RENEW_DAYS)
        if until and until > renew_by:
            return class_room, room_id
        try:
            updates = {
                "room_acs_valid_until": _naive_utc(acs.extend_room(room_id)).isoformat(),
                "room_updated_at": _now(),
            }
            firestore_classes.add_document(str(class_room["id"]), updates)
            return {**class_room, **updates}, room_id
        except acs.AcsError as exc:
            logger.info("Room %s of class %s could not be extended (%s); making a new one.",
                        room_id, class_room.get("id"), exc)

    # `replace` even with no old room: a class can carry its /call/ link with no ACS room
    # behind it yet (one moved over from Google Meet), and create_room would otherwise take
    # the link as a room that already exists.
    updated, error = create_room(
        class_room, SimpleNamespace(id=None, email=None),
        auto_record=class_room.get("room_auto_record") is not False, replace=True,
    )
    if error:
        raise acs.AcsError(error)
    return updated, updated["room_acs_room_id"]


def set_manual_room(class_room: dict, link: str, actor) -> dict:
    """
    Records a link the school already has - Zoom, Teams, anything outside the LMS.

    Nothing is created or armed: the LMS does not own that conference, so it cannot record
    it or collect a recording afterwards. An LMS room made earlier is deleted first.
    """
    cleaned = (link or "").strip()
    if not cleaned.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Enter a full link, starting with https://")

    _drop_room(class_room)
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
    _drop_room(class_room)
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


def ensure_rooms_for_today(reference: datetime | None = None,
                           class_ids: set[int] | None = None,
                           dry_run: bool = False) -> dict:
    """
    Makes sure every class with lessons today has its room before the first one starts.

    Run by the school maintenance sweep every few minutes. A class is due once the clock is
    within `ROOM_LEAD_MINUTES` of its first period and until its last period ends; a class
    that already has a room costs nothing, so in practice this fires once per class ever -
    and again after an admin removes a room. Nothing is done when the classroom model is
    switched off or live classes are not configured.

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
    if not result["class_room_mode"] or not acs.is_enabled():
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
        if dry_run:
            result["created"].append({"class": name, "first_period": first_start.isoformat()})
            continue

        updated, error = create_room(class_room, SimpleNamespace(id=None, email=None), auto_record=True)
        if error:
            result["failed"].append({"class": name, "error": error})
        else:
            result["created"].append({
                "class": name, "room_link": updated.get("room_link"),
                "first_period": first_start.isoformat(),
            })

    if result["created"] and not dry_run:
        logger.info("Class rooms made ready for today: %s",
                    ", ".join(c["class"] for c in result["created"]))
    return result


def meeting_fields_for_room(class_room: dict) -> dict:
    """
    What a session stores when it uses the class's room instead of a link of its own.

    The room's link, so the session's join button opens the class's call, and its recording
    setting, because a teacher's period in the room is recorded against their session
    (`app.services.calls`).
    """
    return {
        "meeting_link": class_room.get("room_link"),
        "google_event_id": None,
        "google_calendar_id": None,
        "meet_status": MEET_STATUS_CLASS_ROOM,
        "meet_error": None,
        "meet_space_name": None,
        "meet_meeting_code": None,
        "meet_owner_email": None,
        "uses_class_room": True,
        "auto_record": bool(class_room.get("room_auto_record")),
        "recording_status": class_room.get("room_recording_status") or "NOT_REQUESTED",
        "recording_error": class_room.get("room_recording_error"),
        "meet_access_type": None,
        "meet_access_error": None,
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
                # Due but not there: the sweep has not run yet, or ACS refused it.
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

    # An LMS room logs the join when the call actually connects (app/services/calls.py), so
    # somebody whose camera prompt failed is not counted in. A pasted external link is the
    # one case where handing the link out is all the LMS will ever see.
    if class_room.get("room_provider") != PROVIDER_ACS:
        log_join(class_room, user, access)

    return {
        "class_id": access["class_id"],
        "class_name": access["class_name"],
        "room_link": class_room.get("room_link"),
        "access": {**access, "room_link": class_room.get("room_link")},
    }


def _period_detail(access: dict) -> str | None:
    current = access.get("current_period")
    if not current:
        return None
    return f"during {(current['entry'].get('subject') or {}).get('name') or 'a period'}"


def log_join(class_room: dict, user, access: dict | None = None) -> None:
    access = access or access_for(class_room, user)
    log_event(class_room["id"], EVENT_JOINED_ROOM, user, detail=_period_detail(access))


def leave_room(class_room: dict, user) -> dict:
    """
    Records that this person has left the room.

    The call page reports it when somebody hangs up; the "Leave the class" button does the
    same for a pasted external link, whose tab the LMS cannot see close.
    """
    access = access_for(class_room, user)
    log_event(class_room["id"], EVENT_LEFT_ROOM, user, detail=_period_detail(access))
    return access_for(class_room, user)


# ---------------------------------------------------------------------------------------
# Attendance: join and leave times per person, per day
#
# Each row is one person's day in the room: every stretch between a join and the next leave,
# when they first came and last went, and the minutes in between. Built from the room log,
# which for an LMS room is written by the call itself on connect and hang-up. Stored in
# `class_room_attendance` so the office can read a past day without recomputing it.
# ---------------------------------------------------------------------------------------

ATTENDANCE_SYNC_MINUTES = 15


def attendance_for_class(class_id, on_date) -> list[dict]:
    """The attendance rows for one class on one day, earliest joiner first."""
    key = on_date.isoformat() if hasattr(on_date, "isoformat") else str(on_date)
    rows = [
        r for r in firestore_class_room_attendance.query_documents("class_id", "==", int(class_id))
        if str(r.get("date")) == key
    ]
    rows.sort(key=lambda r: (str(r.get("first_joined_at") or ""), str(r.get("display_name") or "")))
    return rows


def sync_room_attendance(class_room: dict, on_date=None, force: bool = False) -> tuple[list[dict], str | None]:
    """
    Rebuilds one day's attendance rows for a class from its room log.

    Throttled to once every `ATTENDANCE_SYNC_MINUTES` per class unless forced. Returns
    `(rows for the day, error)`; the error is always None now that nothing outside the LMS
    has to be asked - it is kept for the callers that report it.
    """
    class_id = int(class_room["id"])
    tz = timetable_service.school_timezone()
    day = on_date or datetime.now(tz).date()

    now_utc = datetime.utcnow()
    if not force:
        last = _parse_stored(class_room.get("room_attendance_synced_at"))
        if last and now_utc - last < timedelta(minutes=ATTENDANCE_SYNC_MINUTES):
            return attendance_for_class(class_id, day), None

    day_start_local = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    since_utc = day_start_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    until_utc = (day_start_local + timedelta(days=1)).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    events = [
        e for e in events_for_class(class_id, since=since_utc, limit=5000)
        if e.get("user_id") is not None and e.get("action") in PRESENCE_EVENTS
        and (_parse_stored(e.get("at")) or until_utc) < until_utc
    ]
    events.sort(key=lambda e: str(e.get("at") or ""))

    people: dict[int, dict] = {}
    for event in events:
        uid = int(event["user_id"])
        person = people.setdefault(uid, {
            "name": event.get("user_name"), "role": event.get("role"), "sessions": [], "open": None,
        })
        at = _parse_stored(event.get("at"))
        if event["action"] in JOIN_EVENTS:
            if person["open"] is None:
                person["open"] = at
        elif person["open"] is not None:
            person["sessions"].append((person["open"], at))
            person["open"] = None

    synced = now_utc.isoformat()
    for uid, person in people.items():
        sessions = list(person["sessions"])
        if person["open"] is not None:
            sessions.append((person["open"], None))
        minutes = sum(
            ((end or now_utc) - start).total_seconds() / 60.0
            for start, end in sessions if start and (end or now_utc) > start
        )
        leaves = [end for _, end in sessions if end]
        firestore_class_room_attendance.add_document(f"{class_id}_{day.isoformat()}_{uid}", {
            "class_id": class_id,
            "date": day.isoformat(),
            "conference_record": None,
            "conference_start_at": None,
            "conference_end_at": None,
            "participant": f"users/{uid}",
            "display_name": person["name"],
            "user_kind": "LMS",
            "matched_user_id": uid,
            "matched_user_name": person["name"],
            "matched_role": person["role"],
            "sessions": [
                {"joined_at": start.isoformat() if start else None, "left_at": end.isoformat() if end else None}
                for start, end in sessions
            ],
            "first_joined_at": sessions[0][0].isoformat() if sessions and sessions[0][0] else None,
            "last_left_at": max(leaves).isoformat() if leaves else None,
            "still_in": person["open"] is not None,
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
        if not has_room(class_room):
            continue
        periods = timetable_service.resolve_on_date(
            timetable_service.list_entries(class_id=int(class_room["id"])), now.date(), reference=now
        )
        if not periods or now < periods[0]["starts_at"]:
            continue
        name = class_room.get("name") or f"Class {class_room.get('id')}"
        try:
            sync_room_attendance(class_room)
            result["synced"].append(name)
        except Exception as exc:  # one class's bad data must not stop the rest
            logger.warning("Attendance for class %s not rebuilt: %s", name, exc)
            result["failed"].append({"class": name, "error": str(exc)})
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
