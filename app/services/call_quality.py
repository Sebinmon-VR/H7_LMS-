"""
How the calls went: who kept dropping out, whose network or microphone was failing.

The call page reports two things here (app/api/v1/calls.py):
  - why each call ended - the ACS end code, so a dropped connection (4xx/5xx) can be told
    from somebody hanging up (0);
  - the warnings ACS raises about the person's own side while they are in the call: a poor
    or lost network, a microphone that stopped working, a camera that froze, no speaker.

They live in their own table, `call_quality_events`, not in the room log: the room log
decides who is in a room and who attended, and a burst of network warnings must never
push a join out of what that reads. The office reads them back per class and day as one
line per person, worst first (see `summary`).
"""

import logging
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta

from fastapi import HTTPException

from app.core import acs
from app.core.firebase import firestore_call_quality_events
from app.services import class_rooms
from app.services import timetable as timetable_service

logger = logging.getLogger("call_quality")

EVENT_ENDED = "callEnded"

# The warnings the page may send: ACS's own diagnostic names. Anything else is refused,
# so the table holds only what the office's view knows how to read.
# audioReceiveQuality is the page's own measure, not an ACS warning: the sound arriving had
# packets lost or patched over ("Poor" or "Bad"), which is what a class hears as choppy voices.
NETWORK_ISSUES = frozenset({
    "networkReconnect", "networkReceiveQuality", "networkSendQuality", "noNetwork", "networkRelaysNotReachable",
    "audioReceiveQuality",
})
DEVICE_ISSUES = frozenset({
    "noSpeakerDevicesEnumerated", "noMicrophoneDevicesEnumerated", "microphoneNotFunctioning",
    "microphoneMuteUnexpectedly", "microphonePermissionDenied", "cameraFreeze", "cameraStartFailed",
    "cameraStartTimedOut", "cameraStoppedUnexpectedly", "cameraPermissionDenied",
    "capturerStartFailed", "capturerStoppedUnexpectedly",
})
ISSUES = NETWORK_ISSUES | DEVICE_ISSUES


def _class_id_for(kind: str, entity_id: str) -> int | None:
    """The class a call belongs to, when it belongs to one: a class room, or a class's meeting."""
    if kind == acs.KIND_CLASS:
        return int(entity_id)
    if kind == acs.KIND_MEETING:
        from app.services import live_classes

        meeting = live_classes.require_meeting(int(entity_id))
        return int(meeting["class_id"]) if meeting.get("class_id") is not None else None
    return None


def _school_today() -> date:
    return datetime.now(timetable_service.school_timezone()).date()


def _record(kind: str, entity_id: str, user, name: str, value: str, detail: str | None = None) -> None:
    """One line in the table. Never raises: a report must not break the call page."""
    try:
        class_id = _class_id_for(kind, entity_id)
        role = getattr(user, "role", None)
        firestore_call_quality_events.add_document(str(firestore_call_quality_events.get_next_numeric_id()), {
            "class_id": class_id,
            "kind": kind,
            "entity_id": str(entity_id),
            "user_id": int(user.id),
            "user_name": getattr(user, "full_name", None),
            "role": getattr(role, "value", role),
            "name": name,
            "value": value,
            "detail": detail,
            "day": _school_today().isoformat(),
            "at": datetime.utcnow().isoformat(),
        })
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - a log line must not fail the request
        logger.warning("Call quality event %s for %s %s not recorded: %s", name, kind, entity_id, exc)


def record_issue(kind: str, entity_id: str, user, name: str, value: str) -> dict:
    """A warning ACS raised on this person's side of the call."""
    if name not in ISSUES:
        raise HTTPException(status_code=422, detail=f"Unknown call warning: {name}")
    if kind == acs.KIND_CLASS:
        class_rooms.assert_may_see(class_rooms.require_class(int(entity_id)), user)
    _record(kind, entity_id, user, name, (value or "")[:20])
    return {"ok": True}


def record_end(kind: str, entity_id: str, user, code: int | None, sub_code: int | None) -> None:
    """Why this person's call ended. 0 is hanging up; 4xx and 5xx are failures."""
    if code is None:
        return
    _record(kind, entity_id, user, EVENT_ENDED, f"{code}/{sub_code if sub_code is not None else 0}")


def is_drop(value: str | None) -> bool:
    """An end code that means the connection failed rather than somebody leaving."""
    try:
        code = int(str(value or "").split("/")[0])
    except ValueError:
        return False
    return code >= 400 and code not in (401, 403, 404)


def summary(class_id: int, day: date | None = None) -> dict:
    """
    One line per person who was in the class's calls that day: how often they joined,
    how many of their calls dropped, and which warnings they had, worst first.
    """
    day = day or _school_today()
    rows = [
        r for r in firestore_call_quality_events.query_documents("class_id", "==", int(class_id))
        if r.get("day") == day.isoformat()
    ]

    tz = timetable_service.school_timezone()
    start = class_rooms._school_day_start_utc(datetime.combine(day, time(12), tz))
    end = start + timedelta(days=1)
    joins: Counter = Counter()
    people: dict[int, dict] = {}
    for event in class_rooms.events_for_class(int(class_id), since=start, limit=10000):
        if str(event.get("at") or "") >= end.isoformat():
            continue
        uid = event.get("user_id")
        if uid is None:
            continue
        people.setdefault(int(uid), {"name": event.get("user_name"), "role": event.get("role")})
        if event.get("action") in class_rooms.JOIN_EVENTS:
            joins[int(uid)] += 1

    issues: dict[int, Counter] = defaultdict(Counter)
    drops: Counter = Counter()
    hangups: Counter = Counter()
    end_codes: dict[int, Counter] = defaultdict(Counter)
    last_at: dict[int, str] = {}
    for r in rows:
        uid = int(r["user_id"])
        people.setdefault(uid, {"name": r.get("user_name"), "role": r.get("role")})
        if r.get("name") == EVENT_ENDED:
            if is_drop(r.get("value")):
                drops[uid] += 1
                end_codes[uid][r.get("value")] += 1
            else:
                hangups[uid] += 1
        else:
            issues[uid][r["name"]] += 1
        if str(r.get("at") or "") > last_at.get(uid, ""):
            last_at[uid] = str(r.get("at"))

    lines = []
    for uid, who in people.items():
        rejoins = max(0, joins[uid] - 1)
        network = sum(n for k, n in issues[uid].items() if k in NETWORK_ISSUES)
        device = sum(n for k, n in issues[uid].items() if k in DEVICE_ISSUES)
        lines.append({
            "user_id": uid,
            "name": who.get("name") or f"User {uid}",
            "role": who.get("role"),
            "joins": joins[uid],
            "rejoins": rejoins,
            "drops": drops[uid],
            "network_warnings": network,
            "device_warnings": device,
            "issues": dict(issues[uid]),
            "end_codes": dict(end_codes[uid]),
            "last_problem_at": last_at.get(uid),
            # How much trouble, for the order: a dropped call counts most, then rejoining,
            # then the warnings.
            "score": drops[uid] * 5 + rejoins * 3 + network + device * 2,
        })
    lines.sort(key=lambda line: (-line["score"], (line["name"] or "").lower()))
    return {"class_id": int(class_id), "day": day.isoformat(), "people": lines}
