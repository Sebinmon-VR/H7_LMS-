"""
Who is online: the last moment each signed-in user did anything.

Every authenticated request touches the user's presence row - throttled to one write a
minute per user per process - and an open tab sends a heartbeat once a minute, so somebody
reading a page that never polls still counts. "Online" is a last touch inside
ONLINE_WINDOW_SECONDS. Nobody announces leaving (a closed laptop tells no one), so offline
is simply silence: about three minutes of it.

The rows live in their own collection rather than on the user document, so a write a
minute per active user never churns the cached user list.
"""

import logging
import threading
import time
from datetime import datetime, timedelta

from app.core.firebase import firestore_user_presence

logger = logging.getLogger(__name__)

# The heartbeat is every 60 s; three missed beats and the person is treated as gone.
ONLINE_WINDOW_SECONDS = 180
TOUCH_EVERY_SECONDS = 60

_last_touch: dict[int, float] = {}
_lock = threading.Lock()


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(tz=None).replace(tzinfo=None) if False else parsed.replace(tzinfo=None)
    return parsed


def touch(user, force: bool = False) -> bool:
    """
    Records a sign of life for `user`. Returns True when a write happened.

    Throttled per process so a busy dashboard polling every few seconds costs one write a
    minute, not one per request. Never raises: presence is not worth failing a request.
    """
    user_id = int(getattr(user, "id", 0) or 0)
    if not user_id:
        return False

    now_mono = time.monotonic()
    with _lock:
        if not force and now_mono - _last_touch.get(user_id, -1e9) < TOUCH_EVERY_SECONDS:
            return False
        _last_touch[user_id] = now_mono

    role = getattr(user, "role", None)
    try:
        firestore_user_presence.add_document(str(user_id), {
            "user_id": user_id,
            "role": getattr(role, "value", role),
            "full_name": getattr(user, "full_name", None),
            "last_seen_at": datetime.utcnow().isoformat(),
        })
        return True
    except Exception as exc:  # noqa: BLE001 - a presence write must never break a request
        logger.warning("Presence touch failed for user %s: %s", user_id, exc)
        with _lock:
            _last_touch.pop(user_id, None)
        return False


def snapshot(window_seconds: int = ONLINE_WINDOW_SECONDS) -> list[dict]:
    """Every user's last sign of life, online first, then most recent first."""
    cutoff = datetime.utcnow() - timedelta(seconds=window_seconds)
    rows = []
    for doc in firestore_user_presence.list_all():
        seen = _parse(doc.get("last_seen_at"))
        if doc.get("user_id") is None:
            continue
        rows.append({
            "user_id": int(doc["user_id"]),
            "last_seen_at": seen,
            "is_online": bool(seen and seen >= cutoff),
        })
    rows.sort(key=lambda r: (not r["is_online"], -(r["last_seen_at"].timestamp() if r["last_seen_at"] else 0.0)))
    return rows
