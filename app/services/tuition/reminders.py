"""
Tuition class reminders, and the sweep that keeps the calendar current.

This is the LMS reminder design applied to one-to-one classes, and it keeps the two
properties that matter, because the failure mode of a reminder system is not silence - it is
spam:

  * **Exactly once.** Before sending, the sweep *claims* a Firestore document keyed by
    (session, offset, recipient) with an atomic create. A second worker, a restart mid-sweep,
    or two overlapping sweeps all lose the race and skip. The claim is written before the
    email goes out, so the worst case is a dropped reminder rather than a duplicate - the
    right way round for something that lands in somebody's inbox.
  * **No catch-up floods.** A reminder that came due more than the lateness window ago is
    skipped entirely, so a server that was down all evening comes back quiet instead of
    emailing everyone about classes that already finished.

Two things differ from the LMS version, and both come from the product:

  * Reminders read from **sessions**, not from the recurring rules. A tuition class can be
    rescheduled or cancelled individually, and a reminder resolved from the weekly pattern
    would cheerfully tell a student to attend a class that was called off yesterday.
  * Every reminder is rendered in the **recipient's own timezone**. A student in London and
    their teacher in Dubai are reminded about the same class and must each be told the time
    they will actually see on their own clock.

The same sweep also does two housekeeping jobs, because they need the same cadence and
running them together costs one thread instead of three: it extends the generated-session
horizon, and it closes classes nobody ended.
"""

import logging
import threading
from datetime import datetime, timedelta

from app.core.enums import TuitionSessionStatus
from app.core.firebase import (
    firestore_subjects, firestore_tuition_reminder_log, firestore_tuition_sessions,
    firestore_users,
)
from app.core.mailer import is_configured as mail_is_configured, send_class_reminder_email
from app.services.tuition.common import now_utc, to_utc, user_timezone
from app.services.tuition.settings_store import tuition_settings

logger = logging.getLogger("tuition.reminders")

# How a reminder time is written in the email body, in the recipient's own zone.
TIME_LABEL_FORMAT = "%A %d %B, %H:%M"

_scheduler: "TuitionScheduler | None" = None


def _claim_key(session_id, offset: int, user_id) -> str:
    return f"{session_id}_{offset}_{user_id}"


def due_sessions(reference: datetime | None = None) -> list[dict]:
    """
    Classes entering a reminder window right now.

    Reads only classes still expected to happen. A cancelled class reminds nobody, and a
    class already under way needs no nudge - the people in it are in it.
    """
    config = tuition_settings()
    now = reference or now_utc()
    offsets = config["reminder_minutes_before"]
    lateness = config["reminder_max_lateness_minutes"]
    if not offsets:
        return []

    horizon = now + timedelta(minutes=max(offsets) + 1)
    due = []

    for session in firestore_tuition_sessions.query_documents(
        "status", "==", TuitionSessionStatus.SCHEDULED.value
    ):
        starts = to_utc(session.get("scheduled_start_at"))
        if not starts or starts > horizon:
            continue

        for offset in offsets:
            fires_at = starts - timedelta(minutes=offset)
            if fires_at > now:
                continue
            if (now - fires_at).total_seconds() / 60.0 > lateness:
                continue
            due.append({**session, "offset_minutes": offset, "starts_at": starts})
            # One offset per class per sweep. The tighter offsets are still pending and will
            # fire on a later pass; sending several at once would defeat the point of having
            # more than one.
            break

    return due


def _recipients(session: dict, config: dict) -> list[dict]:
    people = []
    student = firestore_users.get_document(str(session.get("student_id")))
    if student and student.get("email") and student.get("reminder_opt_in", True) is not False:
        people.append(student)

    if config["remind_teachers"]:
        teacher = firestore_users.get_document(str(session.get("teacher_id")))
        if teacher and teacher.get("email") and teacher.get("reminder_opt_in", True) is not False:
            people.append(teacher)

    return people


def sweep(reference: datetime | None = None, send: bool = True) -> dict:
    """
    One reminder pass.

    `send=False` makes it a dry run, which is what the admin's preview endpoint uses: an
    admin who has just changed the lead time wants to see what would go out before it does.
    A dry run claims nothing, so it can be run as often as anybody likes.
    """
    config = tuition_settings()
    if not config["reminders_enabled"]:
        return {"enabled": False, "sent": 0, "detail": "Tuition reminders are switched off."}

    if send and not mail_is_configured():
        return {"enabled": True, "sent": 0, "detail": "Email is not configured; nothing sent."}

    sent = failed = skipped = 0
    previews = []

    for session in due_sessions(reference):
        subject = firestore_subjects.get_document(str(session.get("subject_id"))) or {}
        teacher = firestore_users.get_document(str(session.get("teacher_id"))) or {}
        offset = session["offset_minutes"]

        for person in _recipients(session, config):
            key = _claim_key(session.get("id"), offset, person.get("id"))

            if not send:
                previews.append({
                    "session_id": session.get("id"),
                    "to": person.get("email"),
                    "offset_minutes": offset,
                    "starts_at": session["starts_at"].isoformat(),
                })
                continue

            # The claim, before the email. See the module docstring.
            claimed = firestore_tuition_reminder_log.create_document(key, {
                "session_id": session.get("id"),
                "user_id": person.get("id"),
                "email": person.get("email"),
                "offset_minutes": offset,
                "scheduled_start_at": session["starts_at"].isoformat(),
                "claimed_at": datetime.utcnow().isoformat(),
            })
            if not claimed:
                skipped += 1
                continue

            # Each recipient is told the time on their own clock.
            local = session["starts_at"].astimezone(user_timezone(person))
            delivered = send_class_reminder_email(
                to=person["email"],
                full_name=person.get("full_name", ""),
                subject_name=subject.get("name", "your class"),
                class_name="One-to-one tuition",
                starts_at_label=f"{local.strftime(TIME_LABEL_FORMAT)} ({local.tzname()})",
                minutes_before=offset,
                teacher_name=teacher.get("full_name"),
                meeting_link=session.get("meeting_link"),
            )
            if delivered:
                sent += 1
            else:
                failed += 1
                # The claim is left in place. A retry that re-sent on the next pass would
                # risk delivering twice for a transient SMTP failure that actually went
                # through, which is the worse of the two outcomes.
                firestore_tuition_reminder_log.add_document(key, {"delivery_failed": True})

    if not send:
        return {"enabled": True, "dry_run": True, "would_send": len(previews), "previews": previews}

    return {
        "enabled": True,
        "sent": sent,
        "failed": failed,
        "already_sent": skipped,
        "detail": f"{sent} sent, {failed} failed, {skipped} already claimed.",
    }


def maintenance_pass() -> dict:
    """
    The housekeeping that runs alongside reminders: extend the horizon, close stale classes.

    Both are idempotent and cheap, and both need to happen on roughly the reminder cadence -
    a class that ended an hour ago should not still read as in progress, and the calendar
    should never run out of generated classes while somebody is looking at it.
    """
    from app.services.tuition.scheduling import generate_sessions
    from app.services.tuition.sessions import auto_start_due_sessions, close_stale_sessions

    generated = generate_sessions()
    # Opening due classes runs before closing stale ones so a class cannot be started and
    # settled in the same pass, which would record a lesson nobody was ever in.
    auto_started = auto_start_due_sessions()
    closed = close_stale_sessions()
    return {"sessions_generated": generated, "sessions_auto_started": auto_started, **closed}


class TuitionScheduler:
    """
    Daemon thread running the tuition sweep on an interval.

    A second scheduler beside the LMS one rather than a branch inside it. They read different
    collections, honour different admin-set lead times, and either must be able to be turned
    off without touching the other - which a shared thread with two code paths makes
    needlessly delicate.

    Each server worker runs its own, which is safe for exactly the reason it is safe in the
    LMS: the Firestore claim makes duplicate delivery impossible.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.started_at: datetime | None = None
        self.last_run_at: datetime | None = None
        self.last_result: dict | None = None
        self.last_maintenance: dict | None = None
        self.last_error: str | None = None
        self.run_count = 0

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        from app.core.config import settings as env_settings

        if self.is_running:
            return False
        if not env_settings.ENABLE_TUITION_MODULE:
            logger.info("Tuition module is disabled (ENABLE_TUITION_MODULE=False).")
            return False

        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="tuition-scheduler", daemon=True)
        self._thread.start()
        self.started_at = datetime.utcnow()
        logger.info("Tuition scheduler started.")
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        from app.core.config import settings as env_settings

        interval = max(float(env_settings.REMINDER_SCAN_INTERVAL_SECONDS), 15.0)
        while not self._stop.is_set():
            try:
                self.last_result = sweep()
                self.last_maintenance = maintenance_pass()
                self.last_error = None
            except Exception as exc:
                # A failure must never kill the thread, or reminders and session generation
                # stop silently until somebody restarts the server.
                self.last_error = str(exc)
                logger.exception("Tuition sweep failed")
            finally:
                self.last_run_at = datetime.utcnow()
                self.run_count += 1

            self._stop.wait(interval)

    def status(self) -> dict:
        config = tuition_settings()
        return {
            "running": self.is_running,
            "timezone": config["timezone"],
            "reminders_enabled": config["reminders_enabled"],
            "offsets_minutes": config["reminder_minutes_before"],
            "remind_teachers": config["remind_teachers"],
            "mail_configured": mail_is_configured(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "run_count": self.run_count,
            "last_error": self.last_error,
            "last_result": self.last_result,
            "last_maintenance": self.last_maintenance,
            "server_time_utc": now_utc().isoformat(),
        }


def get_scheduler() -> TuitionScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = TuitionScheduler()
    return _scheduler
