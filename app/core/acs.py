"""
Azure Communication Services: the live-class video that replaced Google Meet.

What the LMS asks of ACS is small, and all of it is here:

* **Identities.** Every LMS user who joins a call gets one ACS identity, created on first
  need and remembered on their profile (`acs_user_id`). It is an opaque id, not an email, so
  it does not matter what address - personal Gmail or school - somebody signs in with.
* **Rooms.** An ACS room is an access-controlled call: only identities added to it may
  enter, each with a role. A standing class room, a scheduled session and a tuition class
  each own one. The LMS decides *when* somebody may join (the timetable rules in
  app/services/class_rooms.py and live_classes.py); ACS only enforces *whether* they are on
  the list, and the LMS adds them to it at the moment it lets them in.
* **Tokens.** The browser joins with a short-lived calling token for its identity.
* **Recording.** Call Recording is started and stopped by the server; finished files arrive
  through Event Grid and are handled in app/services/recordings.py.

Every function raises AcsError on failure so callers can decide whether a failure costs a
request (joining) or only a log line (tidying up a room).
"""

import logging
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from app.core.config import settings

logger = logging.getLogger("acs")

KIND_CLASS = "class"
KIND_MEETING = "meeting"
KIND_TUITION = "tuition"
KINDS = (KIND_CLASS, KIND_MEETING, KIND_TUITION)

ROLE_PRESENTER = "Presenter"
ROLE_ATTENDEE = "Attendee"

PROVIDER = "AZURE_ACS"

_clients: dict[str, object] = {}
_lock = threading.Lock()


class AcsError(Exception):
    """ACS is unconfigured or refused the request."""


def is_configured() -> bool:
    return bool(settings.ACS_CONNECTION_STRING.strip())


def is_enabled() -> bool:
    return bool(settings.ENABLE_ACS_CALLING and is_configured())


def endpoint() -> str | None:
    """The resource's endpoint, read out of the connection string."""
    for part in settings.ACS_CONNECTION_STRING.split(";"):
        key, _, value = part.partition("=")
        if key.strip().lower() == "endpoint" and value:
            return value.strip().rstrip("/")
    return None


def resource_host() -> str | None:
    """Identifies the resource identities belong to; an identity is useless on another one."""
    url = endpoint()
    return urlsplit(url).netloc.lower() if url else None


def configuration_problems() -> list[str]:
    problems = []
    if not settings.ENABLE_ACS_CALLING:
        problems.append("ENABLE_ACS_CALLING is off, so live classes cannot be joined in the LMS.")
    if not is_configured():
        problems.append(
            "ACS_CONNECTION_STRING is not set. Copy it from the Communication Services "
            "resource: Settings -> Keys -> Connection string."
        )
    elif not endpoint():
        problems.append("ACS_CONNECTION_STRING has no endpoint= part.")
    return problems


def recording_problems() -> list[str]:
    problems = []
    if not settings.ENABLE_AUTO_RECORDING:
        problems.append("ENABLE_AUTO_RECORDING is off.")
    if not settings.ACS_EVENTS_KEY:
        problems.append(
            "ACS_EVENTS_KEY is not set, so the recording webhook refuses every delivery. Set "
            "it, then subscribe Event Grid (RecordingFileStatusUpdated) to "
            "/api/v1/calls/events?key=<ACS_EVENTS_KEY>."
        )
    from app.core import azure_blob

    if not azure_blob.is_configured():
        problems.append("Blob storage is not configured, so finished recordings have nowhere to go.")
    return problems


def _client(kind: str):
    if not is_configured():
        raise AcsError("Azure Communication Services is not configured (ACS_CONNECTION_STRING).")
    with _lock:
        client = _clients.get(kind)
        if client is None:
            conn = settings.ACS_CONNECTION_STRING
            if kind == "identity":
                from azure.communication.identity import CommunicationIdentityClient

                client = CommunicationIdentityClient.from_connection_string(conn)
            elif kind == "rooms":
                from azure.communication.rooms import RoomsClient

                client = RoomsClient.from_connection_string(conn)
            elif kind == "calls":
                from azure.communication.callautomation import CallAutomationClient

                client = CallAutomationClient.from_connection_string(conn)
            else:  # pragma: no cover - programming error
                raise ValueError(kind)
            _clients[kind] = client
    return client


def call_link(kind: str, entity_id) -> str:
    """
    Where a class is joined: a page of the LMS itself. Stored in the same fields that held
    Meet links (`room_link`, `meeting_link`), so every screen that opened a link keeps working.
    """
    return f"/call/{kind}/{entity_id}"


def is_call_link(link: str | None) -> bool:
    return bool(link) and str(link).startswith("/call/")


def _utc(value) -> datetime:
    """An aware UTC datetime. The rooms SDK hands `valid_until` back as an ISO string."""
    if isinstance(value, str):
        text = value.replace("Z", "+00:00")
        if "." in text:
            # Seven fractional digits come back; fromisoformat takes at most six.
            head, _, tail = text.partition(".")
            digits = "".join(ch for ch in tail if ch.isdigit())
            text = f"{head}.{digits[:6]}{tail[len(digits):]}"
        value = datetime.fromisoformat(text)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


# ---------------------------------------------------------------------------------------
# Identities and tokens
# ---------------------------------------------------------------------------------------

def identity_for(user: dict) -> str:
    """
    The ACS identity of an LMS user, created and saved on first need.

    Stored with the resource it belongs to: an id minted on one Communication resource is
    refused by another, so moving the school to a new resource quietly re-issues them.
    """
    from app.core.firebase import firestore_users

    host = resource_host()
    existing = user.get("acs_user_id")
    if existing and user.get("acs_resource") == host:
        return existing

    try:
        created = _client("identity").create_user()
    except AcsError:
        raise
    except Exception as exc:
        raise AcsError(f"Could not create a calling identity: {exc}") from exc

    acs_user_id = created.properties["id"]
    firestore_users.add_document(str(user["id"]), {"acs_user_id": acs_user_id, "acs_resource": host})
    user["acs_user_id"], user["acs_resource"] = acs_user_id, host
    return acs_user_id


def issue_token(acs_user_id: str) -> dict:
    """A VoIP calling token for one identity: {"token", "expires_on"}."""
    from azure.communication.identity import CommunicationTokenScope, CommunicationUserIdentifier

    try:
        token = _client("identity").get_token(
            CommunicationUserIdentifier(acs_user_id),
            scopes=[CommunicationTokenScope.VOIP],
            token_expires_in=timedelta(hours=max(1, min(int(settings.ACS_TOKEN_HOURS), 24))),
        )
    except Exception as exc:
        raise AcsError(f"Could not issue a calling token: {exc}") from exc
    return {"token": token.token, "expires_on": _utc(token.expires_on)}


# ---------------------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------------------

def create_room(valid_until: datetime | None = None) -> dict:
    """A new room, open from now. Returns {"room_id", "valid_until"}."""
    now = datetime.now(timezone.utc)
    until = _utc(valid_until) if valid_until else now + timedelta(days=settings.ACS_ROOM_VALIDITY_DAYS)
    until = min(until, now + timedelta(days=180))
    try:
        room = _client("rooms").create_room(
            valid_from=now - timedelta(minutes=5),
            valid_until=until,
            pstn_dial_out_enabled=False,
        )
    except AcsError:
        raise
    except Exception as exc:
        raise AcsError(f"Could not create a room: {exc}") from exc
    return {"room_id": room.id, "valid_until": _utc(room.valid_until)}


def extend_room(room_id: str, valid_until: datetime | None = None) -> datetime:
    """Pushes a room's end out. Raises AcsError when the room is gone or already expired."""
    now = datetime.now(timezone.utc)
    until = _utc(valid_until) if valid_until else now + timedelta(days=settings.ACS_ROOM_VALIDITY_DAYS)
    until = min(until, now + timedelta(days=180))
    try:
        room = _client("rooms").update_room(room_id=room_id, valid_until=until)
    except Exception as exc:
        raise AcsError(f"Could not extend room {room_id}: {exc}") from exc
    return _utc(room.valid_until)


def delete_room(room_id: str | None) -> bool:
    """Best-effort: a room that cannot be deleted simply expires."""
    if not room_id or not is_configured():
        return False
    try:
        _client("rooms").delete_room(room_id=room_id)
        return True
    except Exception as exc:
        logger.warning("Room %s not deleted: %s", room_id, exc)
        return False


def add_participant(room_id: str, acs_user_id: str, role: str) -> None:
    """Puts an identity on a room's list with a role, or updates its role."""
    from azure.communication.rooms import CommunicationUserIdentifier, ParticipantRole, RoomParticipant

    wanted = ParticipantRole.PRESENTER if role == ROLE_PRESENTER else ParticipantRole.ATTENDEE
    try:
        _client("rooms").add_or_update_participants(
            room_id=room_id,
            participants=[RoomParticipant(
                communication_identifier=CommunicationUserIdentifier(acs_user_id), role=wanted,
            )],
        )
    except Exception as exc:
        raise AcsError(f"Could not add the participant to room {room_id}: {exc}") from exc


def check_access() -> dict:
    """Live probe: can rooms be listed with this connection string?"""
    problems = configuration_problems()
    info = {"provider": PROVIDER, "endpoint": endpoint(), "problems": problems}
    if problems:
        return {**info, "ok": False, "detail": problems[0]}
    try:
        next(iter(_client("rooms").list_rooms()), None)
        return {**info, "ok": True, "detail": "Azure Communication Services answered."}
    except Exception as exc:
        return {**info, "ok": False, "detail": f"Azure Communication Services refused the request: {exc}"}


def describe_configuration() -> dict:
    return {
        "provider": PROVIDER,
        "enabled": settings.ENABLE_ACS_CALLING,
        "configured": is_configured(),
        "endpoint": endpoint(),
        "room_validity_days": settings.ACS_ROOM_VALIDITY_DAYS,
        "token_hours": settings.ACS_TOKEN_HOURS,
        "problems": configuration_problems(),
        "recording": {
            "enabled": settings.ENABLE_AUTO_RECORDING,
            "events_key_set": bool(settings.ACS_EVENTS_KEY),
            "problems": recording_problems(),
        },
    }


# ---------------------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------------------

def start_recording(server_call_id: str | None = None, room_id: str | None = None) -> str:
    """Starts a mixed audio+video MP4 recording of a live call. Returns the recording id."""
    from azure.communication.callautomation import (
        RecordingChannel, RecordingContent, RecordingFormat, RoomCallLocator, ServerCallLocator,
    )

    if server_call_id:
        locator = ServerCallLocator(server_call_id)
    elif room_id:
        locator = RoomCallLocator(room_id)
    else:
        raise AcsError("A recording needs the call's server id or its room id.")
    try:
        properties = _client("calls").start_recording(
            call_locator=locator,
            recording_content_type=RecordingContent.AUDIO_VIDEO,
            recording_channel_type=RecordingChannel.MIXED,
            recording_format_type=RecordingFormat.MP4,
        )
    except AcsError:
        raise
    except Exception as exc:
        raise AcsError(f"Could not start recording: {exc}") from exc
    return properties.recording_id


def stop_recording(recording_id: str) -> bool:
    """Stops a recording. An already-stopped one counts as success."""
    try:
        _client("calls").stop_recording(recording_id)
        return True
    except Exception as exc:
        text = str(exc).lower()
        if "not found" in text or "8522" in text or "inactive" in text:
            return True
        logger.warning("Recording %s not stopped: %s", recording_id, exc)
        return False


def download_recording(content_location: str):
    """The bytes of one finished recording chunk, streamed."""
    try:
        result = _client("calls").download_recording(content_location)
        # azure-communication-callautomation 1.6 hands back the HTTP response itself
        # rather than the byte iterator it is documented to return (SDK issue #42835).
        if hasattr(result, "iter_bytes"):
            result.raise_for_status()
            return result.iter_bytes()
        return result
    except Exception as exc:
        raise AcsError(f"Could not download the recording: {exc}") from exc
