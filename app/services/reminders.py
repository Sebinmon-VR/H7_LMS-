"""
Class reminders driven by the timetable.

A background sweep runs every REMINDER_SCAN_INTERVAL_SECONDS, works out which timetabled
periods are entering a reminder window, and emails the enrolled students (and optionally the
teacher). CLASS_REMINDER_MINUTES_BEFORE may list several offsets, so a school can send both
a "in an hour" and a "in 5 minutes" nudge.

Two properties matter more than anything else here, because the failure mode of a reminder
system is not silence - it is spam:

  * **Exactly once.** Before sending, the sweep *claims* a Firestore document keyed by
    (entry, date, offset, recipient) using an atomic create. A second worker, a restart
    mid-sweep, or two overlapping sweeps all lose the race and skip. The claim is written
    before the email goes out, so the worst case is a dropped reminder rather than a
    duplicate one - the right way round for something that lands in someone's inbox.
  * **No catch-up floods.** A period whose reminder became due more than
    REMINDER_MAX_LATENESS_MINUTES ago is skipped entirely. A server that was down all
    morning comes back and stays quiet instead of emailing everyone about classes that
    already finished.
"""

import logging
import threading
from datetime import date, datetime, timedelta
from typing import Callable

from app.core.config import settings
from app.core.enums import UserRole
from app.core.firebase import (
    firestore_classes, firestore_meetings, firestore_reminder_log,
    firestore_student_enrollments, firestore_subjects, firestore_timetable, firestore_users,
)
from app.core.mailer import is_configured as mail_is_configured, send_class_reminder_email
from app.services.timetable import (
    entry_active_on, now_local, parse_time, school_timezone,
)

logger = logging.getLogger("reminders")

# How a reminder time is written in the email body.
TIME_LABEL_FORMAT = "%A %d %B, %H:%M"

_scheduler: "ReminderScheduler | None" = None


def _reminder_key(entry_id, on_date: date, offset: int, user_id) -> str:
    return f"{entry_id}_{on_date.isoformat()}_{offset}_{user_id}"


class _SweepContext:
    """
    Per-sweep lookup cache.

    A sweep resolves the same handful of classes, subjects and teachers for every recipient
    of every period. Reading them once per sweep keeps the whole pass to a small, constant
    number of Firestore round trips regardless of how many students are being emailed.
    """

    def __init__(self) -> None:
        self._classes: dict = {}
        self._subjects: dict = {}
        self._users: dict = {}
        self._students_by_class: dict = {}
        self._meetings_by_class: dict = {}

    def class_room(self, class_id):
        if class_id not in self._classes:
            self._classes[class_id] = firestore_classes.get_document(str(class_id)) or {}
        return self._classes[class_id]

    def subject(self, subject_id):
        if subject_id not in self._subjects:
            self._subjects[subject_id] = firestore_subjects.get_document(str(subject_id)) or {}
        return self._subjects[subject_id]

    def user(self, user_id):
        if user_id is None:
            return {}
        if user_id not in self._users:
            self._users[user_id] = firestore_users.get_document(str(user_id)) or {}
        return self._users[user_id]

    def students_in_class(self, class_id) -> list[dict]:
        if class_id in self._students_by_class:
            return self._students_by_class[class_id]

        enrollments = firestore_student_enrollments.query_documents("class_id", "==", class_id)
        student_ids = [e["student_id"] for e in enrollments if e.get("student_id") is not None]
        resolved = firestore_users.get_documents(student_ids)

        students = [
            student for student in (resolved.get(str(sid)) for sid in student_ids)
            if student and student.get("is_active", False)
        ]
        self._students_by_class[class_id] = students
        return students

    def meeting_link(self, class_id, subject_id, starts_at: datetime) -> str | None:
        """
        A live meeting link for this period, when one has been scheduled for the same slot.

        Turns the reminder into something actionable for an online class rather than a note
        about a room the student is not walking to.
        """
        if class_id not in self._meetings_by_class:
            self._meetings_by_class[class_id] = firestore_meetings.query_documents(
                "class_id", "==", class_id
            )

        window_start = starts_at - timedelta(minutes=30)
        window_end = starts_at + timedelta(minutes=30)

        for meeting in self._meetings_by_class[class_id]:
            if meeting.get("subject_id") != subject_id or not meeting.get("meeting_link"):
                continue
            scheduled = meeting.get("scheduled_time")
            if not scheduled:
                continue
            try:
                when = datetime.fromisoformat(str(scheduled))
            except ValueError:
                continue
            if when.tzinfo is None:
                when = when.replace(tzinfo=starts_at.tzinfo)
            if window_start <= when <= window_end:
                return meeting["meeting_link"]
        return None


def _wants_reminders(user: dict) -> bool:
    """Reminders are opt-out: only an explicit False suppresses them."""
    return user.get("reminder_opt_in") is not False


def _recipients(entry: dict, context: _SweepContext) -> list[dict]:
    """Everyone who should hear about a period: enrolled students, and optionally the teacher."""
    people = [
        student for student in context.students_in_class(entry.get("class_id"))
        if student.get("email") and _wants_reminders(student)
    ]

    if settings.REMIND_TEACHERS and entry.get("teacher_id") is not None:
        teacher = context.user(entry.get("teacher_id"))
        if (
            teacher.get("email")
            and teacher.get("is_active", False)
            and _wants_reminders(teacher)
            and teacher.get("role") in (UserRole.TEACHER.value, UserRole.ADMIN.value)
        ):
            people.append(teacher)

    # A teacher enrolled in their own class would otherwise be emailed twice.
    unique: dict = {}
    for person in people:
        unique.setdefault(person.get("id"), person)
    return list(unique.values())


def due_periods(reference: datetime | None = None) -> list[dict]:
    """
    Every (period, offset) pair whose reminder is due right now.

    A reminder is due when its send time has passed but by no more than
    REMINDER_MAX_LATENESS_MINUTES, and the period itself has not already started.
    """
    tz = school_timezone()
    reference = reference or datetime.now(tz)
    lateness = timedelta(minutes=max(settings.REMINDER_MAX_LATENESS_MINUTES, 0))

    entries = [e for e in firestore_timetable.list_all() if e.get("is_active", True)]
    if not entries:
        return []

    # A reminder can be due for tomorrow's first period when the offset is large, so the
    # window has to look past midnight rather than only at today.
    candidate_dates = [
        (reference + timedelta(days=offset_days)).date() for offset_days in (0, 1)
    ]

    due: list[dict] = []
    for entry in entries:
        try:
            start = parse_time(entry.get("start_time"))
            end = parse_time(entry.get("end_time"))
        except ValueError:
            logger.warning("Timetable entry %s has an unreadable time; skipping.", entry.get("id"))
            continue

        for on_date in candidate_dates:
            if not entry_active_on(entry, on_date):
                continue

            starts_at = datetime.combine(on_date, start, tzinfo=tz)
            ends_at = datetime.combine(on_date, end, tzinfo=tz)
            if ends_at <= reference:
                continue

            for offset in settings.reminder_offsets:
                send_at = starts_at - timedelta(minutes=offset)
                if send_at <= reference <= send_at + lateness:
                    due.append({
                        "entry": entry,
                        "on_date": on_date,
                        "offset": offset,
                        "starts_at": starts_at,
                        "ends_at": ends_at,
                    })

    due.sort(key=lambda item: item["starts_at"])
    return due


def sweep(
    reference: datetime | None = None,
    dry_run: bool = False,
    progress: Callable[[int, str], None] | None = None,
) -> dict:
    """
    Finds due reminders and sends them. Returns a summary suitable for a job result.

    `dry_run` reports exactly what would be sent without claiming or emailing anything,
    which is how the admin endpoint lets someone check a timetable change before the next
    sweep acts on it.
    """
    def report(percent: int, message: str) -> None:
        if progress:
            progress(percent, message)

    started = datetime.utcnow()
    report(5, "Finding due reminders")

    summary = {
        "due_periods": 0,
        "recipients": 0,
        "sent": 0,
        "skipped_already_sent": 0,
        "failed": 0,
        "dry_run": dry_run,
        "mail_configured": mail_is_configured(),
        "reference": (reference or now_local()).isoformat(),
        "offsets": settings.reminder_offsets,
        "details": [],
    }

    if not settings.ENABLE_CLASS_REMINDERS:
        summary["detail"] = "ENABLE_CLASS_REMINDERS is False; nothing was sent."
        return summary

    if not dry_run and not summary["mail_configured"]:
        summary["detail"] = (
            "SMTP is not configured, so no reminder could be delivered. Set SMTP_USER and "
            "SMTP_PASSWORD (a Google App Password) to enable delivery."
        )
        return summary

    pending = due_periods(reference)
    summary["due_periods"] = len(pending)
    if not pending:
        summary["detail"] = "No class reminders were due."
        return summary

    context = _SweepContext()
    report(30, f"Preparing {len(pending)} reminder window(s)")

    for index, item in enumerate(pending):
        entry = item["entry"]
        offset = item["offset"]
        starts_at = item["starts_at"]

        subject_name = context.subject(entry.get("subject_id")).get("name") or "your class"
        class_name = context.class_room(entry.get("class_id")).get("name") or "your class"
        teacher_name = context.user(entry.get("teacher_id")).get("full_name")
        link = context.meeting_link(entry.get("class_id"), entry.get("subject_id"), starts_at)
        starts_label = starts_at.strftime(TIME_LABEL_FORMAT)

        people = _recipients(entry, context)
        summary["recipients"] += len(people)

        entry_sent = 0
        for person in people:
            key = _reminder_key(entry["id"], item["on_date"], offset, person.get("id"))

            if dry_run:
                if firestore_reminder_log.get_document(key):
                    summary["skipped_already_sent"] += 1
                else:
                    entry_sent += 1
                continue

            claimed = firestore_reminder_log.create_document(key, {
                "timetable_entry_id": entry["id"],
                "user_id": person.get("id"),
                "email": person.get("email"),
                "class_id": entry.get("class_id"),
                "subject_id": entry.get("subject_id"),
                "on_date": item["on_date"].isoformat(),
                "minutes_before": offset,
                "starts_at": starts_at.isoformat(),
                "claimed_at": datetime.utcnow().isoformat(),
                "status": "SENDING",
            })
            if not claimed:
                summary["skipped_already_sent"] += 1
                continue

            delivered = send_class_reminder_email(
                to=person["email"],
                full_name=person.get("full_name") or person["email"],
                subject_name=subject_name,
                class_name=class_name,
                starts_at_label=starts_label,
                minutes_before=offset,
                teacher_name=teacher_name,
                room=entry.get("room"),
                meeting_link=link,
            )

            firestore_reminder_log.add_document(key, {
                "status": "SENT" if delivered else "FAILED",
                "completed_at": datetime.utcnow().isoformat(),
            })

            if delivered:
                summary["sent"] += 1
                entry_sent += 1
            else:
                summary["failed"] += 1

        summary["details"].append({
            "timetable_entry_id": entry["id"],
            "class": class_name,
            "subject": subject_name,
            "starts_at": starts_at.isoformat(),
            "minutes_before": offset,
            "recipients": len(people),
            "sent": entry_sent,
        })

        report(30 + int(60 * (index + 1) / len(pending)), f"Processed {index + 1}/{len(pending)}")

    verb = "would be sent" if dry_run else "sent"
    summary["detail"] = (
        f"{summary['sent']} reminder(s) {verb} across {summary['due_periods']} period(s); "
        f"{summary['skipped_already_sent']} already handled, {summary['failed']} failed."
    )
    summary["duration_seconds"] = round((datetime.utcnow() - started).total_seconds(), 2)
    report(100, summary["detail"])
    return summary


class ReminderScheduler:
    """
    Daemon thread that calls `sweep()` on an interval.

    Held in memory like the job registry, with the same honest limitation: each server
    worker runs its own scheduler. That is safe rather than merely tolerable here, because
    the Firestore claim makes duplicate delivery impossible - a second worker's sweep finds
    every reminder already claimed and does nothing.
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
        if not settings.ENABLE_CLASS_REMINDERS:
            logger.info("Class reminders are disabled (ENABLE_CLASS_REMINDERS=False).")
            return False

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="lms-reminders", daemon=True
        )
        self._thread.start()
        self.started_at = datetime.utcnow()
        logger.info(
            "Class reminder scheduler started: every %.0fs, offsets %s, timezone %s.",
            settings.REMINDER_SCAN_INTERVAL_SECONDS,
            settings.reminder_offsets,
            settings.resolved_school_timezone,
        )
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        interval = max(float(settings.REMINDER_SCAN_INTERVAL_SECONDS), 15.0)
        while not self._stop.is_set():
            try:
                self.last_result = sweep()
                self.last_error = None
                if self.last_result.get("sent"):
                    logger.info("Reminder sweep: %s", self.last_result["detail"])
            except Exception as exc:
                # A sweep failure must never kill the thread, or reminders stop silently
                # until someone restarts the server.
                self.last_error = str(exc)
                logger.exception("Reminder sweep failed")
            finally:
                self.last_run_at = datetime.utcnow()
                self.run_count += 1

            self._stop.wait(interval)

    def status(self) -> dict:
        return {
            "enabled": settings.ENABLE_CLASS_REMINDERS,
            "running": self.is_running,
            "timezone": settings.resolved_school_timezone,
            "offsets_minutes": settings.reminder_offsets,
            "scan_interval_seconds": settings.REMINDER_SCAN_INTERVAL_SECONDS,
            "max_lateness_minutes": settings.REMINDER_MAX_LATENESS_MINUTES,
            "remind_teachers": settings.REMIND_TEACHERS,
            "mail_configured": mail_is_configured(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "run_count": self.run_count,
            "last_error": self.last_error,
            "last_result": self.last_result,
            "server_time_local": now_local().isoformat(),
        }


def get_scheduler() -> ReminderScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = ReminderScheduler()
    return _scheduler
