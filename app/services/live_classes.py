"""
The clock around a school live class: when it starts, when you may join, how long is left.

The tuition product already has all of this (`app.services.tuition.sessions`). This gives the
same behaviour to school meetings, and it is a separate module rather than a shared one
because the two hang off different records - a tuition session is one student's class with a
teacher, a school meeting is a class of thirty - and the only thing they genuinely share is
the arithmetic, which is small.

**The timings are computed here, not in the frontend.** A teacher's screen and a student's
screen must not be able to disagree about when a class ends, and the moment that number is
derived on the client it depends on the client's clock. `timing_view` is the one place it
comes from.

**Join gating.** `assert_may_join` refuses before the class opens, which is what makes the
join button safe to disable: the button is a courtesy, the check is the rule. Without the
server-side half, a student who bookmarks the Meet link joins an empty room twenty minutes
early and the teacher finds them already there.

Three things decide whether a class has begun, and they are deliberately distinguishable:

  * **auto-start on** - the class opens on the timetable, whether or not anybody turned up.
  * **auto-start off** - it opens when the teacher starts it; a student before that is
    *waiting*, not late, and telling them otherwise turns a fair rule into an unfair one.
  * **the join window** - students may enter a few minutes early, which is a different
    question from whether the class has started.
"""

import logging
import threading
from datetime import datetime, timedelta

from fastapi import HTTPException

from app.core.config import settings
from app.core.enums import UserRole
from app.core.firebase import firestore_meetings, require_document
from app.core.firebase import log_backend_failure
from app.services.tuition.settings_store import lms_settings

logger = logging.getLogger("live_classes")

# Statuses a meeting can be in. Kept as plain strings because the existing LiveMeeting model
# already stores them that way and an enum here would have to be reconciled with the rows
# already in Firestore.
SCHEDULED = "SCHEDULED"
IN_PROGRESS = "IN_PROGRESS"
COMPLETED = "COMPLETED"
CANCELLED = "CANCELLED"

CLOSED_STATUSES = frozenset({COMPLETED, CANCELLED})


def _now() -> datetime:
    return datetime.utcnow()


def _as_datetime(value) -> datetime | None:
    """
    Parses a stored timestamp, tolerating what Firestore hands back.

    Returns None rather than raising on anything unreadable: a meeting with a corrupted time
    should render as "time unknown", not take down the whole timetable request.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def require_meeting(meeting_id) -> dict:
    return require_document(firestore_meetings, meeting_id, "Meeting")


def duration_minutes(meeting: dict, config: dict | None = None) -> int:
    config = config or lms_settings()
    try:
        return int(meeting.get("duration_minutes") or config["default_class_minutes"])
    except (TypeError, ValueError):
        return int(config["default_class_minutes"])


def scheduled_window(meeting: dict, config: dict | None = None) -> tuple[datetime | None, datetime | None]:
    """The class's own start and end, before any late start or extension."""
    config = config or lms_settings()
    start = _as_datetime(meeting.get("scheduled_time"))
    if start is None:
        return None, None
    return start, start + timedelta(minutes=duration_minutes(meeting, config))


def class_started_at(meeting: dict, config: dict | None = None) -> datetime | None:
    """
    When the class actually began, or None if it has not.

    Under auto-start this is the scheduled time, derived rather than read from the status:
    the sweep that stamps IN_PROGRESS runs on a cadence, and a class that plainly started
    four minutes ago must not be reported as not started because a background thread is
    behind. With auto-start off it is when the teacher actually started it.
    """
    config = config or lms_settings()

    started = _as_datetime(meeting.get("started_at"))
    if started:
        return started

    if config["auto_start_class"]:
        start, _ = scheduled_window(meeting, config)
        return start
    return None


def timing_view(meeting: dict, config: dict | None = None,
                reference: datetime | None = None) -> dict:
    """
    The countdown a client needs, resolved against the clock right now.

    Returned alongside the meeting rather than left to the frontend to derive; see the module
    docstring. `may_join` is the single flag a join button should bind to - it already
    accounts for the early window, the grace period, the status and whether the class has
    begun, so a client that checks anything else will eventually disagree with the server.
    """
    config = config or lms_settings()
    now = reference or _now()

    start, end = scheduled_window(meeting, config)
    began = class_started_at(meeting, config)
    status = meeting.get("status") or SCHEDULED

    # The teacher may overrun; the window stretches to cover it rather than cutting a class
    # off mid-sentence.
    grace = int(config["join_grace_minutes"])
    hard_end = (end + timedelta(minutes=grace)) if end else None

    has_started = bool(began and now >= began)
    is_closed = status in CLOSED_STATUSES
    is_expired = bool(hard_end and now > hard_end)

    opens_at = (
        start - timedelta(minutes=int(config["join_open_minutes_before"]))
        if start else None
    )
    window_open = bool(opens_at and now >= opens_at)

    # A student may enter once the window is open *and* the class has begun. With auto-start
    # on those coincide; with it off the early window only lets them into the waiting state.
    may_join = bool(
        window_open and has_started and not is_closed and not is_expired
    )

    return {
        "now": now.isoformat(),
        "scheduled_start_at": start.isoformat() if start else None,
        "scheduled_end_at": end.isoformat() if end else None,
        "duration_minutes": duration_minutes(meeting, config),
        "starts_in_minutes": (
            round((start - now).total_seconds() / 60.0, 2) if start else None
        ),
        "minutes_remaining": (
            round((end - now).total_seconds() / 60.0, 2) if end else None
        ),
        "class_started_at": began.isoformat() if began else None,
        "class_has_started": has_started,
        "is_live": status == IN_PROGRESS or (has_started and not is_closed and not is_expired),
        "is_closed": is_closed,
        "is_expired": is_expired,

        # --- the join button -----------------------------------------------------------
        "auto_start": config["auto_start_class"],
        "join_opens_at": opens_at.isoformat() if opens_at else None,
        "join_window_open": window_open,
        "may_join": may_join,
        # What a student's screen should say while they wait. A student who arrives for a
        # class the teacher has not opened is waiting, not late.
        "waiting_for_teacher": bool(
            window_open and not has_started and not is_closed and not is_expired
        ),
        "join_blocked_reason": _join_blocked_reason(
            window_open, has_started, is_closed, is_expired, opens_at, config
        ),
    }


def _join_blocked_reason(window_open: bool, has_started: bool, is_closed: bool,
                         is_expired: bool, opens_at: datetime | None,
                         config: dict) -> str | None:
    """
    Why the join button is disabled, in words a student can act on.

    Null when they may join. Returned rather than left to the frontend to compose, because
    "this class has not started yet" and "this class is over" are different messages and a
    client deriving them from flags gets one of them wrong.
    """
    if is_closed:
        return "This class has ended."
    if is_expired:
        return "This class is over and the link has closed."
    if not window_open:
        when = opens_at.strftime("%H:%M") if opens_at else "shortly"
        return f"The class opens at {when}."
    if not has_started:
        if config["auto_start_class"]:
            return "This class has not started yet."
        return "Waiting for the teacher to start the class."
    return None


def assert_may_join(meeting: dict, user, config: dict | None = None) -> dict:
    """
    The server-side half of the join gate. See the module docstring.

    Teachers and admins are exempt from the "has it started?" half - somebody has to be able
    to open the room first, and a teacher locked out of their own class until it starts
    itself is a rule with no way to satisfy it. They are still refused once it is over.
    """
    config = config or lms_settings()
    timing = timing_view(meeting, config)

    role = getattr(user, "role", None)
    is_host = role in (UserRole.ADMIN, UserRole.TEACHER, UserRole.CLASS_TEACHER)

    if timing["is_closed"] or timing["is_expired"]:
        raise HTTPException(
            status_code=409,
            detail=timing["join_blocked_reason"] or "This class is no longer open.",
        )
    if is_host:
        return timing
    if not timing["may_join"]:
        raise HTTPException(status_code=409, detail=timing["join_blocked_reason"])
    return timing


def start_meeting(meeting: dict, actor_id: int | None = None) -> dict:
    """
    Opens the class. Idempotent - starting an already-running class returns it unchanged.

    `started_at` is stamped with the real time rather than the scheduled one, so a class the
    teacher opened eight minutes late is recorded as having done so. Under auto-start this is
    usually never called: the class opens on the timetable and `class_started_at` derives it.
    """
    if meeting.get("status") == IN_PROGRESS:
        return meeting
    if meeting.get("status") in CLOSED_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"This class is already {meeting['status'].lower()}.",
        )

    updates = {
        "status": IN_PROGRESS,
        "started_at": _now().isoformat(),
        "started_by": actor_id,
    }
    firestore_meetings.add_document(str(meeting["id"]), updates)
    logger.info("Started meeting %s.", meeting["id"])
    return {**meeting, **updates}


def end_meeting(meeting: dict, actor_id: int | None = None) -> dict:
    """Closes the class. The link stops working for students immediately afterwards."""
    if meeting.get("status") in CLOSED_STATUSES:
        return meeting

    updates = {
        "status": COMPLETED,
        "ended_at": _now().isoformat(),
        "ended_by": actor_id,
    }
    firestore_meetings.add_document(str(meeting["id"]), updates)
    logger.info("Ended meeting %s.", meeting["id"])
    return {**meeting, **updates}


def auto_start_due_meetings(reference: datetime | None = None) -> int:
    """
    Moves due classes to IN_PROGRESS when the school has asked for automatic starts.

    A convenience, never a correctness dependency: `class_started_at` derives the same answer
    from the timetable, so a student's screen is right even when this sweep is minutes
    behind. A no-op when auto-start is off.

    Does not invent an attendance record or a teacher arrival. Who actually turned up is a
    fact about people, and a sweep asserting it would corrupt the register.
    """
    config = lms_settings()
    if not config["auto_start_class"]:
        return 0

    now = reference or _now()
    started = 0

    for meeting in firestore_meetings.query_documents("status", "==", SCHEDULED):
        start, end = scheduled_window(meeting, config)
        if not start or now < start:
            continue
        # A class whose whole window passed while the server was down is stale, not due.
        if end and now > end + timedelta(minutes=int(config["join_grace_minutes"])):
            continue

        firestore_meetings.add_document(str(meeting["id"]), {
            "status": IN_PROGRESS,
            "started_at": start.isoformat(),
            "auto_started": True,
        })
        started += 1

    if started:
        logger.info("Auto-started %s school class(es).", started)
    return started


def close_stale_meetings(reference: datetime | None = None) -> int:
    """
    Closes classes nobody ended, once their window and grace period have passed.

    Without this a meeting left IN_PROGRESS stays "live" on every student's dashboard
    indefinitely, which trains people to ignore the live badge.
    """
    now = reference or _now()
    config = lms_settings()
    grace = int(config["join_grace_minutes"])
    closed = 0

    for meeting in firestore_meetings.query_documents("status", "==", IN_PROGRESS):
        _, end = scheduled_window(meeting, config)
        if not end or now <= end + timedelta(minutes=grace):
            continue

        firestore_meetings.add_document(str(meeting["id"]), {
            "status": COMPLETED,
            "ended_at": (end + timedelta(minutes=grace)).isoformat(),
            "auto_closed": True,
        })
        closed += 1

    if closed:
        logger.info("Auto-closed %s stale school class(es).", closed)
    return closed


# ---------------------------------------------------------------------------------------
# The maintenance sweep
# ---------------------------------------------------------------------------------------

def sweep(reference: datetime | None = None) -> dict:
    """
    One pass of the school's housekeeping: open due classes, close abandoned ones, and send
    any parent digest that has come due.

    Each part is independently safe to run at any frequency, which is what lets them share
    one cheap timer rather than needing three:

      * auto-start only stamps a status that `class_started_at` already derives;
      * closing stale classes is idempotent once they are past their grace period;
      * the parent digest is claimed per (student, period, week) before it is sent, so the
        first run of the week sends and every later run that week is a no-op.

    That last property is why a weekly report can be driven by an hourly timer without any
    "is it Monday yet?" logic - the claim is the schedule.
    """
    now = reference or _now()
    result = {
        "ran_at": now.isoformat(),
        "auto_started": 0,
        "closed_stale": 0,
        "parent_reports_sent": 0,
        "errors": [],
    }

    # Each step is caught separately. A failure in one must not stop the others, or a bad
    # meeting record silently stops every class in the school from being closed.
    try:
        result["auto_started"] = auto_start_due_meetings(now)
    except Exception as exc:  # pragma: no cover - a sweep must never kill its thread
        log_backend_failure(logger, "Auto-start sweep failed", exc)
        result["errors"].append(f"auto_start: {exc}")

    try:
        result["closed_stale"] = close_stale_meetings(now)
    except Exception as exc:  # pragma: no cover
        log_backend_failure(logger, "Stale-class sweep failed", exc)
        result["errors"].append(f"close_stale: {exc}")

    if settings.ENABLE_PARENT_REPORTS:
        try:
            from app.services import parent_reports

            outcome = parent_reports.sweep(settings.PARENT_REPORT_PERIOD.upper())
            result["parent_reports_sent"] = outcome["emails_sent"]
        except Exception as exc:  # pragma: no cover
            log_backend_failure(logger, "Parent report sweep failed", exc)
            result["errors"].append(f"parent_reports: {exc}")

    return result


class ClassMaintenanceScheduler:
    """
    Daemon thread calling `sweep()` on an interval.

    Modelled on `app.services.reminders.ReminderScheduler`, including its honest limitation:
    each server worker runs its own. That is safe here for the same reason - every action the
    sweep takes is either idempotent or claimed - so a second worker's pass finds the work
    already done.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.started_at: datetime | None = None
        self.last_run_at: datetime | None = None
        self.last_result: dict | None = None
        self.last_error: str | None = None
        self.run_count = 0

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if self.is_running:
            return False

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="lms-class-maintenance", daemon=True
        )
        self._thread.start()
        self.started_at = _now()
        logger.info(
            "Class maintenance scheduler started: every %.0fs.",
            settings.CLASS_MAINTENANCE_INTERVAL_SECONDS,
        )
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        # Floored rather than trusted: a misconfigured interval of 0 would spin the thread
        # against Firestore as fast as it could.
        interval = max(float(settings.CLASS_MAINTENANCE_INTERVAL_SECONDS), 30.0)
        while not self._stop.is_set():
            try:
                self.last_result = sweep()
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                log_backend_failure(logger, "Class maintenance sweep failed", exc)
            finally:
                self.last_run_at = _now()
                self.run_count += 1

            self._stop.wait(interval)

    def status(self) -> dict:
        config = lms_settings()
        return {
            "running": self.is_running,
            "interval_seconds": settings.CLASS_MAINTENANCE_INTERVAL_SECONDS,
            "auto_start_class": config["auto_start_class"],
            "join_open_minutes_before": config["join_open_minutes_before"],
            "join_grace_minutes": config["join_grace_minutes"],
            "parent_reports_enabled": settings.ENABLE_PARENT_REPORTS,
            "parent_report_period": settings.PARENT_REPORT_PERIOD,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "run_count": self.run_count,
            "last_error": self.last_error,
            "last_result": self.last_result,
        }


_scheduler: ClassMaintenanceScheduler | None = None


def get_scheduler() -> ClassMaintenanceScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = ClassMaintenanceScheduler()
    return _scheduler
