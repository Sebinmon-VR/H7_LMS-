"""
Joining a live class inside the LMS.

The call page (/call/<kind>/<id> in the frontend) does three things, and each is one function
here:

  open_session  "Let me in." The same rules as the join buttons decide - the class room's
                timetable windows, a session's clock, a tuition class's student and teacher
                - and only then is the person put on the ACS room's list with their role and
                handed a calling token. A bookmarked link is therefore no way round a rule.
  connected     "I am in." Logged as the join (so the office's board and the attendance view
                reflect who was really in the call), and, for the teacher of a session set to
                record, the recording starts.
  left          "I have left." Logged as the leave; the teacher's recording stops.

Three kinds of thing can be joined, matching the links stored on them:

  class     a school class's standing room      (class_rooms.room_link)
  meeting   a scheduled session with its own room (live_meetings.meeting_link)
  tuition   a one-to-one tuition class          (tuition_sessions.meeting_link)
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.core import acs
from app.core.config import settings
from app.core.enums import UserRole
from app.core.firebase import firestore_meetings, firestore_tuition_sessions, firestore_users
from app.services import class_rooms, live_classes
from app.services import recordings

logger = logging.getLogger("calls")

HOST_ROLES = (UserRole.ADMIN, UserRole.TEACHER, UserRole.CLASS_TEACHER)


@dataclass
class CallTarget:
    """One joinable thing, resolved and cleared for one person."""
    kind: str
    entity_id: str
    title: str
    subtitle: str | None
    room_id: str
    is_host: bool
    # Set when this person's connecting should start a recording, and what to file it under.
    records: bool = False
    class_id: int | None = None
    meeting_id: int | None = None
    # What a recording of this call is filed under in the class library.
    subject_id: int | None = None
    ends_at: datetime | None = None
    extra: dict = field(default_factory=dict)


def _utc_naive(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).replace(tzinfo=None) if moment.tzinfo else moment


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _utc_naive(parsed)


def _require_enabled() -> None:
    if not acs.is_enabled():
        raise HTTPException(
            status_code=503,
            detail="Live classes are not set up on this server. "
                   + " ".join(acs.configuration_problems()),
        )


def _room_for(collection, document: dict, valid_until: datetime | None) -> str:
    """
    The ACS room of a session or tuition class, made on first need and extended while it is
    still wanted. Stored as `acs_room_id` / `acs_room_valid_until` on the document.
    """
    room_id = document.get("acs_room_id")
    until = _parse(document.get("acs_room_valid_until"))
    wanted_until = max(
        _utc_naive(valid_until) or datetime.utcnow(),
        datetime.utcnow() + timedelta(hours=6),
    )
    if room_id and until and until >= wanted_until:
        return room_id
    if room_id:
        try:
            new_until = acs.extend_room(room_id, wanted_until + timedelta(days=1))
            collection.add_document(str(document["id"]), {"acs_room_valid_until": _utc_naive(new_until).isoformat()})
            return room_id
        except acs.AcsError as exc:
            logger.info("Room %s could not be extended (%s); making a new one.", room_id, exc)
    room = acs.create_room(wanted_until + timedelta(days=1))
    collection.add_document(str(document["id"]), {
        "acs_room_id": room["room_id"],
        "acs_room_valid_until": _utc_naive(room["valid_until"]).isoformat(),
    })
    document["acs_room_id"] = room["room_id"]
    return room["room_id"]


# ---------------------------------------------------------------------------------------
# Resolving and clearing each kind
# ---------------------------------------------------------------------------------------

def _class_target(class_id, user) -> CallTarget:
    class_room = class_rooms.require_class(class_id)
    class_rooms.assert_may_see(class_room, user)
    access = class_rooms.access_for(class_room, user)

    if not access["has_room"]:
        if access["is_host"] and access["class_room_mode"]:
            class_room, error = class_rooms.ensure_room(class_room, actor_id=user.id, actor_email=user.email)
            if not class_rooms.has_room(class_room):
                raise HTTPException(status_code=502, detail=error or "The class room could not be created.")
            access = class_rooms.access_for(class_room, user)
        else:
            raise HTTPException(status_code=409, detail=access["join_blocked_reason"])

    if class_room.get("room_provider") != class_rooms.PROVIDER_ACS:
        raise HTTPException(
            status_code=409,
            detail="This class meets at an outside link, not in the LMS. Use the class's join button.",
        )
    if not access["may_join"]:
        raise HTTPException(status_code=409, detail=access["join_blocked_reason"])

    try:
        class_room, room_id = class_rooms.ensure_acs_room(class_room)
    except acs.AcsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    current = access.get("my_current_period") or access.get("current_period")
    subject = ((current or {}).get("entry") or {}).get("subject") or {}

    # A teacher's own period is recorded against their session for it, when there is one:
    # that is where students look for the video. Admins looking in are never recorded.
    meeting_id = None
    is_teacher = user.role in (UserRole.TEACHER, UserRole.CLASS_TEACHER)
    if is_teacher:
        for candidate in access.get("live_meeting_ids") or []:
            meeting = firestore_meetings.get_document(str(candidate)) or {}
            if int(meeting.get("teacher_id") or -1) == int(user.id):
                meeting_id = int(candidate)
                break
    records = bool(
        is_teacher and settings.ENABLE_AUTO_RECORDING and class_room.get("room_auto_record") is not False
        and (meeting_id is not None or access.get("my_current_period") is not None)
    )

    return CallTarget(
        kind=acs.KIND_CLASS,
        entity_id=str(class_room["id"]),
        title=access["class_name"],
        subtitle=subject.get("name"),
        room_id=room_id,
        is_host=bool(access["is_host"]),
        records=records,
        class_id=int(class_room["id"]),
        meeting_id=meeting_id,
        subject_id=((current or {}).get("entry") or {}).get("subject_id"),
        ends_at=access.get("window_closes_at"),
        extra={"class_room": class_room, "access": access},
    )


def _meeting_target(meeting_id, user) -> CallTarget:
    meeting = live_classes.require_meeting(meeting_id)
    live_classes.assert_may_see(meeting, user)
    if meeting.get("uses_class_room") and meeting.get("class_id") is not None:
        # A period in the class's shared room is joined through the room.
        return _class_target(meeting["class_id"], user)
    if not acs.is_call_link(meeting.get("meeting_link")):
        raise HTTPException(
            status_code=409,
            detail="This session meets at an outside link, not in the LMS. Use its join button.",
        )
    timing = live_classes.assert_may_join(meeting, user)

    ends = _parse(timing.get("scheduled_end_at")) or (
        (_parse(meeting.get("scheduled_time")) or datetime.utcnow())
        + timedelta(minutes=int(meeting.get("duration_minutes") or 60))
    )
    try:
        room_id = _room_for(firestore_meetings, meeting, ends)
    except acs.AcsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    is_host = user.role in HOST_ROLES
    is_teacher = int(meeting.get("teacher_id") or -1) == int(user.id)
    subject = None
    if meeting.get("subject_id") is not None:
        from app.core.firebase import firestore_subjects

        subject = (firestore_subjects.get_document(str(meeting["subject_id"])) or {}).get("name")

    return CallTarget(
        kind=acs.KIND_MEETING,
        entity_id=str(meeting["id"]),
        title=meeting.get("title") or "Live class",
        subtitle=subject,
        room_id=room_id,
        is_host=is_host,
        records=bool(is_teacher and settings.ENABLE_AUTO_RECORDING and meeting.get("auto_record")),
        class_id=meeting.get("class_id"),
        meeting_id=int(meeting["id"]),
        subject_id=meeting.get("subject_id"),
        ends_at=ends,
        extra={"meeting": meeting},
    )


def _tuition_target(session_id, user) -> CallTarget:
    from app.services.tuition import sessions as session_service
    from app.services.tuition.common import assert_tuition_access

    session = session_service.require_session(session_id)
    if user.role != UserRole.ADMIN:
        assert_tuition_access(user)
    session_service.assert_may_view(session, user)
    session_service._assert_open(session)
    session_service.assert_student_may_enter(session, user)
    if not acs.is_call_link(session.get("meeting_link")):
        raise HTTPException(
            status_code=409,
            detail="This class meets at an outside link, not in the LMS. Use its join button.",
        )

    ends = _parse(session.get("effective_end_at")) or _parse(session.get("scheduled_end_at"))
    try:
        room_id = _room_for(firestore_tuition_sessions, session, ends)
    except acs.AcsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    from app.core.firebase import firestore_subjects

    subject = (firestore_subjects.get_document(str(session.get("subject_id"))) or {}).get("name")
    is_teacher = int(session.get("teacher_id") or -1) == int(user.id)
    return CallTarget(
        kind=acs.KIND_TUITION,
        entity_id=str(session["id"]),
        title=session.get("title") or subject or "Tuition class",
        subtitle=subject,
        room_id=room_id,
        is_host=is_teacher or user.role == UserRole.ADMIN,
        ends_at=ends,
        extra={"session": session, "is_teacher": is_teacher},
    )


def resolve(kind: str, entity_id: str, user) -> CallTarget:
    if kind == acs.KIND_CLASS:
        return _class_target(int(entity_id), user)
    if kind == acs.KIND_MEETING:
        return _meeting_target(int(entity_id), user)
    if kind == acs.KIND_TUITION:
        return _tuition_target(entity_id, user)
    raise HTTPException(status_code=404, detail="Unknown kind of class.")


# ---------------------------------------------------------------------------------------
# The three calls the page makes
# ---------------------------------------------------------------------------------------

def open_session(kind: str, entity_id: str, user) -> dict:
    """Clears this person for the call, puts them on the room's list and hands out a token."""
    _require_enabled()
    target = resolve(kind, entity_id, user)

    profile = firestore_users.get_document(str(user.id)) or {"id": user.id}
    try:
        acs_user_id = acs.identity_for(profile)
        acs.add_participant(
            target.room_id, acs_user_id, acs.ROLE_PRESENTER if target.is_host else acs.ROLE_ATTENDEE
        )
        token = acs.issue_token(acs_user_id)
    except acs.AcsError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    entity_key = f"{target.kind}:{target.entity_id}"
    return {
        "kind": target.kind,
        "entity_id": target.entity_id,
        "title": target.title,
        "subtitle": target.subtitle,
        "room_id": target.room_id,
        "acs_user_id": acs_user_id,
        "token": token["token"],
        "expires_on": token["expires_on"],
        "display_name": user.full_name or user.email,
        "role": acs.ROLE_PRESENTER if target.is_host else acs.ROLE_ATTENDEE,
        "is_host": target.is_host,
        "records": target.records,
        "recording_active": bool(recordings.active_for(entity_key)),
        "ends_at": target.ends_at,
    }


def connected(kind: str, entity_id: str, user, server_call_id: str | None) -> dict:
    """Logs the join and, for the recording teacher, starts the recording."""
    target = resolve(kind, entity_id, user)

    if target.kind == acs.KIND_CLASS:
        class_rooms.log_join(target.extra["class_room"], user, target.extra["access"])
    elif target.kind == acs.KIND_MEETING and target.class_id is not None:
        class_rooms.log_event(
            target.class_id, class_rooms.EVENT_JOINED_SESSION, user,
            meeting_id=target.meeting_id, detail=target.title,
        )
    elif target.kind == acs.KIND_TUITION:
        from app.services.tuition import sessions as session_service

        session = target.extra["session"]
        if target.extra["is_teacher"]:
            session_service.teacher_join(session, user)
        elif int(session.get("student_id") or -1) == int(user.id):
            session_service.student_join(session, user)

    started = None
    if target.records:
        started = recordings.start(
            kind=target.kind, entity_id=target.entity_id, room_id=target.room_id,
            server_call_id=server_call_id, user=user, title=target.title,
            class_id=target.class_id, meeting_id=target.meeting_id, subject_id=target.subject_id,
        )
    entity_key = f"{target.kind}:{target.entity_id}"
    return {
        "ok": True,
        "recording_started": started is not None,
        "recording_active": bool(recordings.active_for(entity_key)),
    }


def left(kind: str, entity_id: str, user) -> dict:
    """Logs the leave and stops any recording this person started."""
    stopped = 0
    if kind in (acs.KIND_CLASS, acs.KIND_MEETING):
        # Resolved without the join gate: somebody may always say they have gone, even after
        # the window they came in by has closed.
        if kind == acs.KIND_CLASS:
            class_room = class_rooms.require_class(int(entity_id))
            class_rooms.assert_may_see(class_room, user)
            class_rooms.leave_room(class_room, user)
        else:
            meeting = live_classes.require_meeting(int(entity_id))
            live_classes.assert_may_see(meeting, user)
            if meeting.get("uses_class_room") and meeting.get("class_id") is not None:
                kind, entity_id = acs.KIND_CLASS, str(meeting["class_id"])
                class_room = class_rooms.require_class(int(entity_id))
                class_rooms.leave_room(class_room, user)
            elif meeting.get("class_id") is not None:
                class_rooms.log_event(
                    meeting["class_id"], class_rooms.EVENT_LEFT_ROOM, user,
                    meeting_id=meeting["id"], detail=meeting.get("title"),
                )
    stopped = recordings.stop_for(f"{kind}:{entity_id}", user=user)
    return {"ok": True, "recordings_stopped": stopped}


def set_recording(kind: str, entity_id: str, user, action: str, server_call_id: str | None) -> dict:
    """A host's manual record / stop button."""
    target = resolve(kind, entity_id, user)
    if not target.is_host:
        raise HTTPException(status_code=403, detail="Only the teacher can record the class.")
    entity_key = f"{target.kind}:{target.entity_id}"
    if action == "stop":
        stopped = recordings.stop_for(entity_key, reason="Stopped by the teacher.")
        return {"ok": True, "recording_active": bool(recordings.active_for(entity_key)), "stopped": stopped}

    if not settings.ENABLE_AUTO_RECORDING:
        raise HTTPException(status_code=409, detail="Recording is switched off on this server.")
    started = recordings.start(
        kind=target.kind, entity_id=target.entity_id, room_id=target.room_id,
        server_call_id=server_call_id, user=user, title=target.title,
        class_id=target.class_id, meeting_id=target.meeting_id, subject_id=target.subject_id,
    )
    if started is None and not recordings.active_for(entity_key):
        raise HTTPException(status_code=502, detail="The recording could not be started. See the server log.")
    return {"ok": True, "recording_active": True}
