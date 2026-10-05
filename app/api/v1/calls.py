"""
Live classes inside the LMS: the call page's endpoints, and the Event Grid webhook that
brings finished recordings home. See app/services/calls.py for the rules.
"""

import hmac
import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from app.api.v1.dependencies import require_any_authenticated
from app.core import acs
from app.core.config import settings
from app.schemas.user import UserOut
from app.services import calls as call_service
from app.services import recordings

logger = logging.getLogger("calls_api")

router = APIRouter(prefix="/calls", tags=["Live Classes"])

RECORDING_FILE_EVENT = "Microsoft.Communication.RecordingFileStatusUpdated"
VALIDATION_EVENT = "Microsoft.EventGrid.SubscriptionValidationEvent"

CallKind = Literal["class", "meeting", "tuition"]


class CallSessionOut(BaseModel):
    kind: str
    entity_id: str
    title: str
    subtitle: str | None = None
    room_id: str
    acs_user_id: str
    token: str
    expires_on: datetime
    display_name: str
    role: str
    is_host: bool
    records: bool = False
    recording_active: bool = False
    ends_at: datetime | None = None


class CallConnectedIn(BaseModel):
    # The call's server id, read by the page once the call connects. Lets a recording be
    # started against exactly this call and matched to its file afterwards.
    server_call_id: str | None = None


class RecordingActionIn(BaseModel):
    action: Literal["start", "stop"]
    server_call_id: str | None = None


@router.get("/config")
def calls_config(_: UserOut = Depends(require_any_authenticated)):
    """Whether live classes can be joined here, for the page to say so before it tries."""
    return {
        "enabled": acs.is_enabled(),
        "recording_enabled": bool(settings.ENABLE_AUTO_RECORDING),
        "problems": acs.configuration_problems(),
    }


@router.post("/{kind}/{entity_id}/session", response_model=CallSessionOut)
def open_call(kind: CallKind, entity_id: str, user: UserOut = Depends(require_any_authenticated)):
    """
    Let me into this class's call.

    409 with the reason when the class is not open to you now (the same rules as the join
    buttons), 503 when live classes are not configured. On success the page joins the ACS
    room with the returned token; it expires at `expires_on`, and asking again renews it.
    """
    return CallSessionOut(**call_service.open_session(kind, entity_id, user))


@router.post("/{kind}/{entity_id}/connected")
def call_connected(kind: CallKind, entity_id: str, payload: CallConnectedIn,
                   user: UserOut = Depends(require_any_authenticated)):
    """I am in the call. Logged as the join; starts the recording for its teacher."""
    return call_service.connected(kind, entity_id, user, payload.server_call_id)


@router.post("/{kind}/{entity_id}/left")
def call_left(kind: CallKind, entity_id: str, user: UserOut = Depends(require_any_authenticated)):
    """I have left the call. Logged as the leave; stops any recording I started."""
    return call_service.left(kind, entity_id, user)


@router.post("/{kind}/{entity_id}/recording")
def call_recording(kind: CallKind, entity_id: str, payload: RecordingActionIn,
                   user: UserOut = Depends(require_any_authenticated)):
    """[Teacher] Start or stop recording this call by hand."""
    return call_service.set_recording(kind, entity_id, user, payload.action, payload.server_call_id)


# ---------------------------------------------------------------------------------------
# Event Grid
# ---------------------------------------------------------------------------------------

def _check_key(key: str | None) -> None:
    expected = settings.ACS_EVENTS_KEY
    if not expected or not key or not hmac.compare_digest(key, expected):
        raise HTTPException(status_code=401, detail="Unknown event source.")


@router.options("/events", include_in_schema=False)
def events_handshake(request: Request, key: str | None = Query(None)):
    """The CloudEvents schema's abuse-protection handshake."""
    _check_key(key)
    origin = request.headers.get("WebHook-Request-Origin") or "*"
    return Response(status_code=200, headers={
        "WebHook-Allowed-Origin": origin, "WebHook-Allowed-Rate": "120",
    })


@router.post("/events", include_in_schema=False)
async def receive_events(request: Request, background: BackgroundTasks, key: str | None = Query(None)):
    """
    Event Grid deliveries from the Communication resource.

    Subscribe `Microsoft.Communication.RecordingFileStatusUpdated` to this URL with
    `?key=<ACS_EVENTS_KEY>`. The subscription's validation handshake is answered here too.
    The copy into Blob storage runs after the response, because Event Grid gives up on a
    delivery that takes more than thirty seconds and a class video takes longer than that.
    """
    _check_key(key)
    body = await request.json()
    events = body if isinstance(body, list) else [body]

    for event in events:
        event_type = event.get("eventType") or event.get("type")
        if event_type == VALIDATION_EVENT:
            code = (event.get("data") or {}).get("validationCode")
            logger.info("Event Grid subscription validated.")
            return {"validationResponse": code}
        if event_type == RECORDING_FILE_EVENT:
            background.add_task(recordings.process_event_async, {
                "subject": event.get("subject"),
                "data": event.get("data") or {},
            })
    return {"ok": True, "received": len(events)}
