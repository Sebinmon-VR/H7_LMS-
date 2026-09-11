"""
Shared tuition helpers: timezones, program access, and the visibility rules.

## Timezones

The brief asks that every user sees times in their own zone, and that is a harder
requirement than it sounds because tuition mixes two kinds of time that must not be stored
the same way:

  * A **slot** is a wall-clock rule - "Tuesdays at 17:00". It is stored as "HH:MM" in the
    programme's zone and survives daylight saving unchanged, exactly like an LMS timetable
    period.
  * A **session** is an appointment two people in possibly different countries must attend
    simultaneously. It is stored as an absolute UTC instant, because "17:00" means two
    different moments to a teacher in Dubai and a student in London, and only one of them
    can be right.

Resolving a slot into a session is therefore a conversion, not a copy, and `session_start_at`
below is the one place it happens. Display conversion is the mirror image: `localize` adds
`*_local` fields to a response for the reader's own zone, leaving the UTC values in place so
a client that does its own formatting is unaffected.

A user's zone is resolved in three steps - an explicit choice, then where their browser says
they are, then the programme default. See `user_timezone`. That middle step is what makes a
student sitting outside India see their own local time without configuring anything, and it
is fed by the `X-Timezone` header the frontend sets from the browser. Nobody is ever shown a
naive timestamp with no zone attached, which is the failure mode that has people turning up
an hour early.
"""

import logging
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status

from app.core.enums import (
    Program, TEACHING_ROLE_VALUES, UserRole, normalize_programs,
)
from app.services.tuition.settings_store import tuition_settings

logger = logging.getLogger("tuition.common")

TIME_FORMAT = "%H:%M"
UTC = ZoneInfo("UTC")


# ---------------------------------------------------------------------------------------
# Program access
# ---------------------------------------------------------------------------------------

def user_programs(user: Any) -> list[str]:
    """The programs a user may reach, normalized. Missing means LMS-only."""
    raw = getattr(user, "programs", None)
    if raw is None and isinstance(user, dict):
        raw = user.get("programs")
    return normalize_programs(raw)


def _role_value(user: Any) -> str:
    role = getattr(user, "role", None)
    if role is None and isinstance(user, dict):
        role = user.get("role")
    return role.value if isinstance(role, UserRole) else str(role or "")


def has_program(user: Any, program: str = Program.TUITION.value) -> bool:
    """
    Whether a user may use a program at all.

    Administrators pass unconditionally. There is one admin team running both products - the
    brief says so - and an admin locked out of tuition because nobody thought to tick a box
    is a support call, not a security win. Every other role must be explicitly enrolled in
    the program, which is the "special key" the brief asks for.
    """
    if _role_value(user) == UserRole.ADMIN.value:
        return True
    return str(program).upper() in user_programs(user)


def assert_tuition_access(user: Any) -> None:
    """
    Guard for every tuition endpoint. Raises 403 for a user who belongs to the LMS only.

    Deliberately a distinct check from the role guard, and it runs *after* it: role answers
    "may a teacher do this?", program answers "is this teacher one of ours?". A school
    teacher with a valid login and the TEACHER role has no business in the tuition timetable,
    and only this check stops them.
    """
    if not has_program(user, Program.TUITION.value):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is not enrolled in the online tuition programme. "
                   "Ask an administrator to grant tuition access.",
        )


def is_admin(user: Any) -> bool:
    return _role_value(user) == UserRole.ADMIN.value


def is_teacher(user: Any) -> bool:
    return _role_value(user) in TEACHING_ROLE_VALUES


def user_id_of(user: Any) -> int | None:
    value = getattr(user, "id", None)
    if value is None and isinstance(user, dict):
        value = user.get("id")
    return value


# ---------------------------------------------------------------------------------------
# Timezones
# ---------------------------------------------------------------------------------------

def _zone(name: str | None, fallback: ZoneInfo) -> ZoneInfo:
    """
    Resolves an IANA name, falling back rather than raising.

    A user who typed their zone in by hand, or an admin who saved "GMT+4" instead of
    "Asia/Dubai", should see programme time and generate a log line - not a 500 on every
    request they make.
    """
    if not name:
        return fallback
    try:
        return ZoneInfo(str(name).strip())
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        logger.warning("'%s' is not a known IANA timezone; falling back to %s.", name, fallback)
        return fallback


def program_timezone() -> ZoneInfo:
    """The zone tuition slot times are written and read in."""
    return _zone(tuition_settings()["timezone"], UTC)


def _field(user: Any, name: str):
    """Reads a field off either a Pydantic profile or a raw Firestore dict."""
    value = getattr(user, name, None)
    if value is None and isinstance(user, dict):
        value = user.get(name)
    return value


def user_timezone(user: Any) -> ZoneInfo:
    """
    The zone this particular person should see times in.

    Precedence, and the reason for each step:

      1. `timezone` - a zone the person (or an admin) chose deliberately. An explicit choice
         always wins, because somebody who set it meant it.
      2. `detected_timezone` - where their browser last said they were. This is what makes a
         student outside India see their own local time without doing anything, and it
         follows them if they travel.
      3. The programme zone. The right default for the majority who are in it.

    Both are read off the stored profile rather than the request, so the background reminder
    sweep - which has no request to read a header from - tells a student in London about a
    class at the London hour, exactly as the API does.
    """
    return _zone(
        _field(user, "timezone") or _field(user, "detected_timezone"),
        program_timezone(),
    )


def valid_timezone(name: str | None) -> str | None:
    """The IANA name if it is real, otherwise None. Never raises."""
    text = (name or "").strip()
    if not text:
        return None
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None
    return text


def timezone_source(user: Any) -> str:
    """Which of the three rules above decided this person's zone. For the UI to explain itself."""
    if _field(user, "timezone"):
        return "EXPLICIT"
    if _field(user, "detected_timezone"):
        return "DETECTED"
    return "PROGRAMME"


def apply_detected_timezone(user: Any, reported: str | None) -> str | None:
    """
    Records where a user's browser says they are, and uses it when they have no explicit zone.

    Called from the tuition guard on every request, with the `X-Timezone` header a frontend
    sets from `Intl.DateTimeFormat().resolvedOptions().timeZone`. Three properties matter:

      * **It is persisted, not just used.** Reminder emails are sent from a background sweep
        with no request in sight, so a zone that lived only for the duration of one request
        would give a student in London class times in Indian hours by email and London hours
        on screen - the worst of both.
      * **It only writes when the value actually changes.** Otherwise every request from
        every user would be a Firestore write.
      * **It never overrides an explicit choice.** Someone who set their zone by hand keeps
        it, even while travelling; `detected_timezone` is still updated underneath so the UI
        can offer to switch, but nothing moves without them saying so.

    An unrecognized value is ignored rather than rejected. This arrives from a browser API on
    every single request, and a client sending "GMT+5:30" instead of an IANA name should fall
    back to programme time, not fail every call they make.
    """
    detected = valid_timezone(reported)
    if detected is None:
        return None

    if _field(user, "detected_timezone") != detected:
        user_id = user_id_of(user)
        if user_id is not None:
            from app.core.firebase import firestore_users
            firestore_users.add_document(str(user_id), {"detected_timezone": detected})
        try:
            user.detected_timezone = detected
        except (AttributeError, ValueError):
            # A model that does not carry the field still gets the persisted value on its
            # next request; nothing here is worth failing a request over.
            pass

    return detected


def now_utc() -> datetime:
    """Current time as an aware UTC datetime. Aware everywhere, always."""
    return datetime.now(UTC)


def to_utc(value: datetime | str | None) -> datetime | None:
    """
    Reads any stored timestamp back as an aware UTC datetime.

    Naive values are read as UTC. Every write in this module stores UTC, but records written
    by an older revision - or restored from a backup - may be naive, and interpreting those
    as local time would shift them by hours in whichever direction the server happens to sit.
    """
    if value in (None, ""):
        return None
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            logger.warning("Unparseable timestamp '%s'.", value)
            return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def store_dt(value: datetime | None) -> str | None:
    """Serializes an instant for Firestore, always as UTC ISO-8601."""
    moment = to_utc(value)
    return moment.isoformat() if moment else None


def parse_time(value: time | str | None) -> time:
    """Reads a stored 'HH:MM' (or 'HH:MM:SS') slot time."""
    if isinstance(value, time):
        return value
    if not value:
        raise ValueError("Missing time value")
    text = str(value).strip()
    for fmt in (TIME_FORMAT, "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized time value '{value}'. Expected HH:MM.")


def format_time(value: time | str) -> str:
    """Serializes a slot time to the 'HH:MM' form stored in Firestore."""
    if isinstance(value, str):
        return parse_time(value).strftime(TIME_FORMAT)
    return value.strftime(TIME_FORMAT)


def add_minutes_to_time(start: time | str, minutes: int) -> str:
    """
    'HH:MM' plus a duration, wrapping at midnight.

    Wrapping rather than clamping: a 23:30 class of 60 minutes genuinely ends at 00:30, and
    an end time of "23:59" would quietly make a half-hour class look like a half-hour class
    when it is not. Overlap detection handles the wrap explicitly - see `periods_overlap`.
    """
    base = parse_time(start)
    total = (base.hour * 60 + base.minute + int(minutes)) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def parse_date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def session_start_at(on_date: date, start_time: time | str, zone: ZoneInfo | None = None) -> datetime:
    """
    Turns a slot's wall-clock rule into the absolute instant a class begins.

    The conversion is done through the programme zone rather than by adding a fixed offset,
    so a slot written before a daylight-saving change still resolves to 17:00 local on the
    other side of it. This is the single place a slot becomes a moment.
    """
    zone = zone or program_timezone()
    local = datetime.combine(on_date, parse_time(start_time)).replace(tzinfo=zone)
    return local.astimezone(UTC)


def local_naive(value: datetime | str | None, zone: ZoneInfo) -> datetime | None:
    """An instant rendered in a zone, as a naive local datetime for display."""
    moment = to_utc(value)
    return moment.astimezone(zone).replace(tzinfo=None) if moment else None


# The instant fields a tuition response may carry. Listed once so every endpoint localizes
# the same set and none quietly forgets one.
LOCALIZABLE_FIELDS = (
    "scheduled_start_at", "scheduled_end_at", "effective_end_at",
    "teacher_joined_at", "student_joined_at", "started_at", "class_started_at", "ended_at",
)


def localize(record: dict, viewer: Any, fields: Iterable[str] = LOCALIZABLE_FIELDS) -> dict:
    """
    Adds `<field>_local` renderings of a record's instants for one reader.

    Additive on purpose. The UTC values stay exactly as they were, so a client that formats
    timestamps itself is unaffected, and one that would rather be handed the answer reads the
    `_local` field. `viewer_timezone` is included so a UI can label the times it is showing -
    "17:00 (Asia/Dubai)" is useful; a bare "17:00" from an unstated zone is how people miss
    classes.
    """
    zone = user_timezone(viewer)
    enriched = dict(record)
    for name in fields:
        if name in enriched:
            rendered = local_naive(enriched.get(name), zone)
            enriched[f"{name}_local"] = rendered.isoformat() if rendered else None
    enriched["viewer_timezone"] = str(zone)
    return enriched


def localize_many(records: list[dict], viewer: Any,
                  fields: Iterable[str] = LOCALIZABLE_FIELDS) -> list[dict]:
    return [localize(record, viewer, fields) for record in records]


# ---------------------------------------------------------------------------------------
# Overlap arithmetic
#
# Shared by slot conflict detection and by session conflict detection, which ask the same
# question about different representations of a period.
# ---------------------------------------------------------------------------------------

def periods_overlap(start_a: time | str, end_a: time | str,
                    start_b: time | str, end_b: time | str,
                    gap_minutes: int = 0) -> bool:
    """
    True when two same-day wall-clock periods collide.

    Two wrinkles, both of which have produced real double-bookings elsewhere:

      * A period that runs past midnight has an end time *earlier* than its start. Such a
        period is treated as running to the end of the day, so a 23:30-00:30 class still
        clashes with a 23:45 one. The small piece after midnight belongs to the next day and
        is checked there.
      * `gap_minutes` widens each period by the minimum breathing room between classes, so a
        teacher is not booked to finish at 18:00 and start again at 18:00. Touching edges do
        not overlap when the gap is zero, which is the behaviour a back-to-back schedule
        needs.
    """
    a0 = _minutes(start_a)
    a1 = _minutes(end_a)
    b0 = _minutes(start_b)
    b1 = _minutes(end_b)

    if a1 <= a0:
        a1 = 24 * 60
    if b1 <= b0:
        b1 = 24 * 60

    padding = max(int(gap_minutes), 0)
    return a0 - padding < b1 and b0 - padding < a1


def _minutes(value: time | str) -> int:
    parsed = parse_time(value)
    return parsed.hour * 60 + parsed.minute


def instants_overlap(start_a: datetime, end_a: datetime,
                     start_b: datetime, end_b: datetime,
                     gap_minutes: int = 0) -> bool:
    """The same question for two absolute windows. Used for ad-hoc and rescheduled classes."""
    padding = timedelta(minutes=max(int(gap_minutes), 0))
    return start_a - padding < end_b and start_b - padding < end_a


def date_ranges_overlap(from_a, to_a, from_b, to_b) -> bool:
    """
    True when two open-ended effective-date windows intersect.

    Without this, moving a student's Tuesday class to a new teacher mid-term - ending one
    slot on the 14th and starting the replacement on the 15th - would be rejected as a clash
    with the slot it replaces.
    """
    if to_a is not None and from_b is not None and to_a < from_b:
        return False
    if to_b is not None and from_a is not None and to_b < from_a:
        return False
    return True


def slot_active_on(slot: dict, on_date: date) -> bool:
    """True when a recurring slot's rule covers a given calendar date."""
    if not slot.get("is_active", True):
        return False

    starts = parse_date(slot.get("effective_from"))
    ends = parse_date(slot.get("effective_to"))
    if starts and on_date < starts:
        return False
    if ends and on_date > ends:
        return False
    return True
