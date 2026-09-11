"""
Runtime settings an administrator edits without a redeploy.

Environment variables are the right place for credentials and for facts about the *machine*.
They are the wrong place for "remind people 10 minutes before class" and "a class is 45
minutes", which are decisions the person running the school makes, changes their mind about,
and cannot make at all if it needs an engineer and a restart. The brief asks for exactly
these to be settable by the admin - for both products - so they live in Firestore.

The resolution order is: the stored document, then the environment default, then a hardcoded
constant. That ordering matters more than it looks. It means the feature ships working with
no configuration at all, an existing deployment's `.env` keeps deciding behaviour until
somebody actually changes something in the admin UI, and a Firestore outage degrades to the
environment values rather than to nothing.

One document per program, in `app_settings`. Cached, because the reminder sweep reads it
every two minutes and every timezone conversion in the API reads it too.
"""

import logging
from typing import Any

from app.core.config import settings as env_settings
from app.core.enums import Program
from app.core.firebase import document_cache, firestore_app_settings

logger = logging.getLogger("tuition.settings")

TUITION_DOC = Program.TUITION.value.lower()   # "tuition"
LMS_DOC = Program.LMS.value.lower()           # "lms"


def _int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _float(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _bool(value: Any, fallback: bool) -> bool:
    if value is None:
        return fallback
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return fallback


def _offsets(value: Any, fallback: list[int]) -> list[int]:
    """
    Reminder offsets, cleaned up.

    Accepts a list of numbers or a "10, 60" string, because the same setting arrives from a
    JSON body one way and from an environment variable the other. Unreadable entries are
    dropped rather than raising: a bad offset should cost one reminder, not the sweep.
    """
    if value is None:
        return list(fallback)
    if isinstance(value, str):
        tokens = value.replace(",", " ").split()
    else:
        try:
            tokens = list(value)
        except TypeError:
            return list(fallback)

    resolved = set()
    for token in tokens:
        try:
            number = int(token)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            resolved.add(number)
    return sorted(resolved, reverse=True) or list(fallback)


def _stored(program_doc: str) -> dict:
    """The stored overrides for a program, or an empty dict when there are none."""
    try:
        return firestore_app_settings.get_document(program_doc) or {}
    except Exception as exc:  # pragma: no cover - configuration must never 500 a request
        logger.warning("Could not read '%s' settings (%s); using environment defaults.",
                       program_doc, exc)
        return {}


def tuition_settings() -> dict:
    """
    The effective tuition configuration.

    Always returns every key, fully typed. Callers can therefore read
    `tuition_settings()["default_session_minutes"]` without defaulting at each use site,
    which is what keeps the fallback logic in this one file instead of scattered across the
    scheduler, the session service and the reminder sweep.
    """
    stored = _stored(TUITION_DOC)
    return {
        "program": Program.TUITION.value,
        "timezone": (stored.get("timezone") or env_settings.resolved_tuition_timezone).strip(),
        "default_session_minutes": _int(
            stored.get("default_session_minutes"),
            env_settings.TUITION_DEFAULT_SESSION_MINUTES,
        ),
        "reminders_enabled": _bool(
            stored.get("reminders_enabled"), env_settings.ENABLE_TUITION_REMINDERS
        ),
        "reminder_minutes_before": _offsets(
            stored.get("reminder_minutes_before"), env_settings.tuition_reminder_offsets
        ),
        "remind_teachers": _bool(stored.get("remind_teachers"), True),
        "reminder_max_lateness_minutes": _int(
            stored.get("reminder_max_lateness_minutes"),
            env_settings.REMINDER_MAX_LATENESS_MINUTES,
        ),
        "auto_start_class": _bool(
            stored.get("auto_start_class"), env_settings.TUITION_AUTO_START_CLASS
        ),
        "max_teacher_late_extension_minutes": _int(
            stored.get("max_teacher_late_extension_minutes"),
            env_settings.TUITION_MAX_TEACHER_LATE_EXTENSION_MINUTES,
        ),
        "teacher_no_show_minutes": _int(
            stored.get("teacher_no_show_minutes"),
            env_settings.TUITION_TEACHER_NO_SHOW_MINUTES,
        ),
        "student_late_grace_minutes": _int(
            stored.get("student_late_grace_minutes"),
            env_settings.TUITION_STUDENT_LATE_GRACE_MINUTES,
        ),
        "min_gap_minutes": _int(
            stored.get("min_gap_minutes"), env_settings.TUITION_MIN_GAP_MINUTES
        ),
        "session_horizon_days": _int(
            stored.get("session_horizon_days"), env_settings.TUITION_SESSION_HORIZON_DAYS
        ),
        "student_uploads_need_approval": _bool(
            stored.get("student_uploads_need_approval"),
            env_settings.TUITION_STUDENT_UPLOADS_NEED_APPROVAL,
        ),
        "currency": (stored.get("currency") or env_settings.TUITION_CURRENCY).strip() or "INR",
        "default_session_fee": _float(
            stored.get("default_session_fee"), env_settings.TUITION_DEFAULT_SESSION_FEE
        ),
        "auto_create_meet": _bool(stored.get("auto_create_meet"), env_settings.ENABLE_GOOGLE_MEET),
    }


def lms_settings() -> dict:
    """
    The effective LMS reminder configuration.

    Narrower than the tuition equivalent because the LMS's other behaviour is already
    settled by its own module; what the brief asks to be admin-editable here is the reminder
    timing and the timezone, and adding more would be inventing requirements.
    """
    stored = _stored(LMS_DOC)
    return {
        "program": Program.LMS.value,
        "timezone": (stored.get("timezone") or env_settings.resolved_school_timezone).strip(),
        "reminders_enabled": _bool(
            stored.get("reminders_enabled"), env_settings.ENABLE_CLASS_REMINDERS
        ),
        "reminder_minutes_before": _offsets(
            stored.get("reminder_minutes_before"), env_settings.reminder_offsets
        ),
        "remind_teachers": _bool(stored.get("remind_teachers"), env_settings.REMIND_TEACHERS),
        "reminder_max_lateness_minutes": _int(
            stored.get("reminder_max_lateness_minutes"),
            env_settings.REMINDER_MAX_LATENESS_MINUTES,
        ),
    }


def program_settings(program: str) -> dict:
    """Effective settings for either program, by value ('LMS' or 'TUITION')."""
    return lms_settings() if str(program).upper() == Program.LMS.value else tuition_settings()


# Which keys each program will accept from an update. An unknown key is rejected rather than
# stored, so a typo in the admin UI ("remind_teacher") fails loudly at the API instead of
# being written to Firestore and silently ignored forever after.
#
# Written out rather than derived from the resolver functions above: deriving it would mean
# a Firestore read at import time, which is the least reliable moment in the process to
# reach a network service and the exact failure this codebase has been bitten by before.
_LMS_KEYS = frozenset({
    "timezone", "reminders_enabled", "reminder_minutes_before", "remind_teachers",
    "reminder_max_lateness_minutes",
})
WRITABLE_KEYS = {
    Program.LMS.value: _LMS_KEYS,
    Program.TUITION.value: _LMS_KEYS | {
        "default_session_minutes", "auto_start_class", "max_teacher_late_extension_minutes",
        "teacher_no_show_minutes", "student_late_grace_minutes", "min_gap_minutes",
        "session_horizon_days", "student_uploads_need_approval", "currency",
        "default_session_fee", "auto_create_meet",
    },
}


def save_settings(program: str, updates: dict, actor_id: int | None = None) -> dict:
    """
    Merges an admin's changes into a program's settings document.

    Merge rather than replace: the admin UI may only render half these fields, and a PUT
    that dropped the rest would reset behaviour nobody meant to touch. Returns the full
    effective settings afterwards, so the caller sees what actually took effect - including
    the environment values that filled the gaps.
    """
    from datetime import datetime

    program_value = str(program).upper()
    allowed = WRITABLE_KEYS.get(program_value)
    if allowed is None:
        raise ValueError(f"Unknown program '{program}'")

    payload = {key: value for key, value in updates.items() if key in allowed and value is not None}
    if payload:
        payload["updated_at"] = datetime.utcnow().isoformat()
        payload["updated_by"] = actor_id
        firestore_app_settings.add_document(program_value.lower(), payload)
        # add_document invalidates this key already; clearing the whole collection's cache
        # too is cheap here and covers the reminder sweep's separate read path.
        document_cache.invalidate(firestore_app_settings.collection_name)

    return program_settings(program_value)
