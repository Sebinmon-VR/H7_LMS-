"""
Collecting class recordings once the class is over.

Arming a Meet space to record itself (app/core/meet_recordings.py) only solves half the
problem: Meet drops the finished video into the *organiser's personal Drive*, where it is
invisible to students, counts against that teacher's storage, and disappears with them when
they leave. This module is the other half - a background sweep that finds finished sessions,
asks Meet for their recordings, and files each one into the school's Shared Drive under
`Class Recordings/class_<id>/`, shared with the students who were meant to be in the room.

Three properties matter here, because the failure mode of an automated file mover is not
silence - it is duplicate videos and lost originals:

  * **Exactly once.** Before transferring, the sweep *claims* a Firestore document keyed by
    (meeting, Meet recording name) with an atomic create. Two server workers, overlapping
    sweeps, and a restart mid-transfer all lose the race and skip. The claim is written
    before the move, so the worst case is a recording left in the teacher's Drive with a
    claim against it - recoverable by hand, unlike a half-moved file.
  * **No infinite polling.** A class nobody joined produces no recording, ever. Sessions are
    polled until RECORDING_MAX_AGE_HOURS after they ended and then marked UNAVAILABLE, so
    the Meet API cost of the sweep stays proportional to today's timetable rather than to
    the whole history of the school.
  * **Never lose the link.** Every failure downgrades rather than aborts: a video that
    cannot be moved is copied, one that cannot be copied is linked where it lies, and the
    reason lands in `recording_error` where an administrator can read it.

The lifecycle written onto each meeting document as `recording_status`:

    NOT_REQUESTED  auto_record was off for this session
    ARMED          Meet will record it automatically
    ARM_FAILED     arming did not work; still swept, in case a teacher records by hand
    WAITING        the session has ended and Meet has not published a file yet
    STORED         the video is in the school Drive and `recording_url` points at it
    UNAVAILABLE    nothing was ever published; gave up after RECORDING_MAX_AGE_HOURS
    FAILED         a recording exists but could not be filed; `recording_error` says why
"""

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.core import google_drive, meet_recordings
from app.core.config import settings
from app.core.firebase import (
    firestore_meetings, firestore_recording_log, firestore_student_enrollments,
    firestore_users,
)

logger = logging.getLogger("recordings")

# Statuses a sweep will still look at. STORED and UNAVAILABLE are terminal, and
# NOT_REQUESTED means the teacher asked for no recording at all.
OPEN_STATUSES = {None, "", "ARMED", "ARM_FAILED", "WAITING", "FAILED"}

STATUS_NOT_REQUESTED = "NOT_REQUESTED"
STATUS_ARMED = "ARMED"
STATUS_ARM_FAILED = "ARM_FAILED"
STATUS_WAITING = "WAITING"
STATUS_STORED = "STORED"
STATUS_UNAVAILABLE = "UNAVAILABLE"
STATUS_FAILED = "FAILED"

_scheduler: "RecordingScheduler | None" = None


def _parse_time(value) -> datetime | None:
    """
    Reads a timestamp into the naive-UTC frame the rest of this codebase stores.

    Three shapes arrive here: an ISO string written by the LMS, a Firestore timestamp object,
    and an RFC 3339 string with a Z or an offset from the Meet API. All three are normalised
    to naive UTC, because comparing an aware datetime with a naive one raises rather than
    quietly getting the answer wrong - and a sweep that raises stops collecting recordings.
    """
    if isinstance(value, datetime):
        return (
            value.astimezone(timezone.utc).replace(tzinfo=None)
            if value.tzinfo else value
        )
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (
        parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    )


def meeting_ended_at(meeting: dict) -> datetime | None:
    """When a session was scheduled to finish, in the same naive-UTC frame as created_at."""
    started = _parse_time(meeting.get("scheduled_time"))
    if started is None:
        return None
    return started + timedelta(minutes=max(int(meeting.get("duration_minutes") or 60), 1))


def _harvest_window(meeting: dict, reference: datetime) -> tuple[bool, bool]:
    """
    Returns (due_now, too_old) for one meeting.

    `due_now` allows for RECORDING_HARVEST_DELAY_MINUTES of Meet processing after the
    scheduled end. `too_old` is what stops a class nobody attended being polled forever.
    """
    ended = meeting_ended_at(meeting)
    if ended is None:
        return False, False

    due_at = ended + timedelta(minutes=max(settings.RECORDING_HARVEST_DELAY_MINUTES, 0))
    expires_at = ended + timedelta(hours=max(settings.RECORDING_MAX_AGE_HOURS, 1))
    return reference >= due_at, reference > expires_at


def due_meetings(reference: datetime | None = None) -> list[dict]:
    """
    Sessions that have finished and whose recording has not been filed yet.

    Read as one pass over the meetings collection rather than a query per status: the whole
    collection is a few hundred documents in this deployment, and one round trip beats the
    several a compound Firestore query would need without a composite index.
    """
    reference = reference or datetime.utcnow()
    pending = []

    for meeting in firestore_meetings.list_all():
        if meeting.get("recording_status") not in OPEN_STATUSES:
            continue
        if meeting.get("recording_url") and meeting.get("recording_status") == STATUS_STORED:
            continue
        if not (meeting.get("meet_space_name") or meeting.get("meet_meeting_code")):
            # A manually entered link is not a Meet conference we can query.
            continue

        due_now, too_old = _harvest_window(meeting, reference)
        if not due_now:
            continue
        meeting["_too_old"] = too_old
        pending.append(meeting)

    pending.sort(key=lambda m: m.get("scheduled_time") or "")
    return pending


def _teacher_email(meeting: dict) -> str:
    """Email of the teacher the session is filed under, which is who Meet recorded for."""
    teacher = firestore_users.get_document(str(meeting.get("teacher_id")))
    return (teacher or {}).get("email", "")


def _student_emails(class_id) -> list[str]:
    """Active enrolled students' addresses, for sharing the stored recording."""
    if class_id is None:
        return []

    enrollments = firestore_student_enrollments.query_documents("class_id", "==", class_id)
    student_ids = [e.get("student_id") for e in enrollments if e.get("student_id") is not None]
    if not student_ids:
        return []

    resolved = firestore_users.get_documents(student_ids)
    return [
        student["email"]
        for student in (resolved.get(str(sid)) for sid in student_ids)
        if student and student.get("is_active", False) and student.get("email")
    ]


def _claim(meeting_id, recording_name: str) -> bool:
    """
    Atomically claims one (meeting, recording) pair. False means someone else has it.

    Keyed on Meet's own recording resource name, so a session that was recorded in two
    segments files both, and a sweep that runs twice files neither twice.
    """
    key = f"{meeting_id}__{recording_name}".replace("/", "_")
    return firestore_recording_log.create_document(key, {
        "meeting_id": meeting_id,
        "recording_name": recording_name,
        "claimed_at": datetime.utcnow().isoformat(),
        "status": "CLAIMED",
    })


def _record_outcome(meeting_id, recording_name: str, **fields) -> None:
    """Annotates the claim with what happened, so the log answers 'where did it go?'."""
    key = f"{meeting_id}__{recording_name}".replace("/", "_")
    firestore_recording_log.add_document(key, {
        "finished_at": datetime.utcnow().isoformat(), **fields,
    })


def _update_meeting(meeting_id, **fields) -> None:
    firestore_meetings.add_document(str(meeting_id), fields)


def harvest_meeting(meeting: dict, dry_run: bool = False) -> dict:
    """
    Collects and files the recordings of one finished session.

    Returns a per-meeting summary: {"meeting_id", "title", "status", "detail",
    "recording_url", "transferred"}. Never raises for an operational failure - the sweep
    must survive one broken meeting - so the caller can treat every result the same way.
    """
    meeting_id = meeting.get("id")
    title = meeting.get("title") or f"Meeting {meeting_id}"
    summary = {
        "meeting_id": meeting_id,
        "title": title,
        "status": meeting.get("recording_status"),
        "detail": "",
        "recording_url": meeting.get("recording_url"),
        "transferred": 0,
    }

    teacher_email = _teacher_email(meeting)
    listed = meet_recordings.list_recordings(
        teacher_email=teacher_email,
        space_name=meeting.get("meet_space_name"),
        meeting_code=meeting.get("meet_meeting_code"),
    )

    if not listed["ok"]:
        # A session whose window has closed is given up on even when Meet is the thing that
        # is broken. Leaving it open would mean every meeting in the school's history is
        # re-queried on every sweep for as long as the misconfiguration lasts.
        if meeting.get("_too_old"):
            detail = (
                f"Meet could not be queried in the {settings.RECORDING_MAX_AGE_HOURS} hours "
                f"after this session ended, so no recording was collected. {listed['error']}"
            )
            if not dry_run:
                _update_meeting(
                    meeting_id, recording_status=STATUS_UNAVAILABLE, recording_error=detail
                )
            return {**summary, "status": STATUS_UNAVAILABLE, "detail": detail}

        if not dry_run:
            _update_meeting(
                meeting_id, recording_status=STATUS_WAITING, recording_error=listed["error"]
            )
        return {
            **summary,
            "status": STATUS_WAITING,
            "detail": listed["error"] or "Meet could not be queried.",
        }

    recordings = listed["recordings"]

    if not recordings:
        if meeting.get("_too_old"):
            detail = (
                f"No recording was published in the {settings.RECORDING_MAX_AGE_HOURS} hours "
                f"after this session ended; it was probably never joined or never recorded."
            )
            if not dry_run:
                _update_meeting(
                    meeting_id, recording_status=STATUS_UNAVAILABLE, recording_error=detail
                )
            return {**summary, "status": STATUS_UNAVAILABLE, "detail": detail}

        detail = "Meet has not published a recording for this session yet."
        if not dry_run:
            _update_meeting(meeting_id, recording_status=STATUS_WAITING, recording_error=None)
        return {**summary, "status": STATUS_WAITING, "detail": detail}

    if dry_run:
        return {
            **summary,
            "status": "WOULD_STORE",
            "detail": f"{len(recordings)} recording(s) ready to file.",
            "transferred": len(recordings),
        }

    share_with = (
        _student_emails(meeting.get("class_id"))
        if settings.RECORDING_SHARE_WITH_STUDENTS else []
    )
    folder = f"{settings.RECORDING_DRIVE_FOLDER_NAME}/class_{meeting.get('class_id')}"

    stored: list[dict] = []
    warnings: list[str] = []

    for recording in recordings:
        recording_name = recording["recording_name"] or recording["drive_file_id"]

        if not _claim(meeting_id, recording_name):
            logger.info("Recording %s is already claimed; skipping.", recording_name)
            continue

        started = _parse_time(recording.get("start_time")) or _parse_time(
            meeting.get("scheduled_time")
        )
        result = google_drive.transfer_recording(
            drive_file_id=recording["drive_file_id"],
            owner_email=teacher_email,
            folder_path=folder,
            filename=meet_recordings.recording_filename(title, started),
            share_with=share_with,
        )

        _record_outcome(
            meeting_id, recording_name,
            status="STORED" if result["ok"] else "FAILED",
            mode=result["mode"],
            drive_file_id=result["file_id"],
            web_view_link=result["web_view_link"],
            shared_with=len(result["shared_with"]),
            warning=result["warning"],
            error=result["error"],
        )

        if result["ok"]:
            stored.append({**result, "recording": recording})
            if result["warning"]:
                warnings.append(result["warning"])
        else:
            warnings.append(result["error"] or "The recording could not be filed.")

    if not stored:
        detail = " | ".join(warnings) or "Every recording for this session was already filed."
        if warnings:
            _update_meeting(meeting_id, recording_status=STATUS_FAILED, recording_error=detail)
            return {**summary, "status": STATUS_FAILED, "detail": detail}
        return {**summary, "status": meeting.get("recording_status"), "detail": detail}

    # The first stored recording is the canonical one students are pointed at; a session
    # recorded in several segments keeps the rest in `recording_files`.
    primary = stored[0]
    detail = (
        f"{len(stored)} recording(s) filed into Drive as {primary['mode']}."
        + (f" {' | '.join(warnings)}" if warnings else "")
    )

    _update_meeting(
        meeting_id,
        recording_status=STATUS_STORED,
        recording_url=primary["web_view_link"],
        recording_drive_file_id=primary["file_id"],
        recording_transfer_mode=primary["mode"],
        recording_stored_at=datetime.utcnow().isoformat(),
        recording_shared_with=len(primary["shared_with"]),
        recording_error=" | ".join(warnings) or None,
        recording_files=[
            {
                "drive_file_id": item["file_id"],
                "web_view_link": item["web_view_link"],
                "name": item["name"],
                "size_bytes": item["size_bytes"],
                "started_at": item["recording"].get("start_time"),
                "ended_at": item["recording"].get("end_time"),
            }
            for item in stored
        ],
        # A session that finished and produced a video is over, whatever the schedule said.
        status="COMPLETED",
    )

    logger.info("Filed %d recording(s) for meeting %s: %s", len(stored), meeting_id, detail)
    return {
        **summary,
        "status": STATUS_STORED,
        "detail": detail,
        "recording_url": primary["web_view_link"],
        "transferred": len(stored),
    }


def sweep(
    reference: datetime | None = None,
    dry_run: bool = False,
    progress: Callable[[int, str], None] | None = None,
) -> dict:
    """
    Files the recordings of every session that has finished. Returns a job-shaped summary.

    `dry_run` reports what would be filed without claiming, moving or sharing anything, which
    is how the admin endpoint answers "is this actually working?" without touching Drive.
    """
    def report(percent: int, message: str) -> None:
        if progress:
            progress(percent, message)

    summary = {
        "due_meetings": 0,
        "stored": 0,
        "waiting": 0,
        "unavailable": 0,
        "failed": 0,
        "dry_run": dry_run,
        "transfer_mode": settings.recording_transfer_mode,
        "reference": (reference or datetime.utcnow()).isoformat(),
        "details": [],
    }

    problems = meet_recordings.configuration_problems(for_collection=True)
    if problems:
        summary["detail"] = " ".join(problems)
        return summary

    report(5, "Finding finished sessions")
    pending = due_meetings(reference)
    summary["due_meetings"] = len(pending)

    if not pending:
        summary["detail"] = "No finished sessions were waiting for a recording."
        return summary

    for index, meeting in enumerate(pending):
        report(
            10 + int(85 * index / len(pending)),
            f"Checking '{meeting.get('title')}' ({index + 1} of {len(pending)})",
        )
        try:
            result = harvest_meeting(meeting, dry_run=dry_run)
        except Exception as exc:
            # One meeting's failure must not cost the rest of the sweep.
            logger.exception("Recording harvest failed for meeting %s", meeting.get("id"))
            result = {
                "meeting_id": meeting.get("id"),
                "title": meeting.get("title"),
                "status": STATUS_FAILED,
                "detail": str(exc),
                "transferred": 0,
            }

        summary["details"].append(result)
        if result["status"] in (STATUS_STORED, "WOULD_STORE"):
            summary["stored"] += result.get("transferred") or 1
        elif result["status"] == STATUS_WAITING:
            summary["waiting"] += 1
        elif result["status"] == STATUS_UNAVAILABLE:
            summary["unavailable"] += 1
        elif result["status"] == STATUS_FAILED:
            summary["failed"] += 1

    summary["detail"] = (
        f"{summary['due_meetings']} finished session(s): {summary['stored']} recording(s) "
        f"filed, {summary['waiting']} still processing, {summary['unavailable']} never "
        f"recorded, {summary['failed']} failed."
    )
    return summary


class RecordingScheduler:
    """
    Daemon thread that calls `sweep()` on an interval.

    Held in memory like the reminder scheduler, with the same limitation and the same reason
    it is safe: each server worker runs its own, and the Firestore claim makes a duplicate
    transfer impossible - a second worker's sweep finds every recording already claimed and
    does nothing.
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
        if not settings.ENABLE_MEET_AUTO_RECORDING:
            logger.info(
                "Automatic class recording is disabled (ENABLE_MEET_AUTO_RECORDING=False)."
            )
            return False

        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="lms-recordings", daemon=True
        )
        self._thread.start()
        self.started_at = datetime.utcnow()
        logger.info(
            "Class recording sweep started: every %.0fs, %d minutes after each session, "
            "filing as %s into '%s'.",
            settings.RECORDING_SCAN_INTERVAL_SECONDS,
            settings.RECORDING_HARVEST_DELAY_MINUTES,
            settings.recording_transfer_mode,
            settings.RECORDING_DRIVE_FOLDER_NAME,
        )
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        # Meet needs minutes, not seconds, to publish a recording; sweeping faster than this
        # only spends quota re-asking a question whose answer cannot have changed.
        interval = max(float(settings.RECORDING_SCAN_INTERVAL_SECONDS), 60.0)
        while not self._stop.is_set():
            try:
                self.last_result = sweep()
                self.last_error = None
                if self.last_result.get("stored"):
                    logger.info("Recording sweep: %s", self.last_result["detail"])
            except Exception as exc:
                # A sweep failure must never kill the thread, or recordings stop being
                # collected silently until someone restarts the server.
                self.last_error = str(exc)
                logger.exception("Recording sweep failed")
            finally:
                self.last_run_at = datetime.utcnow()
                self.run_count += 1

            self._stop.wait(interval)

    def status(self) -> dict:
        return {
            "enabled": settings.ENABLE_MEET_AUTO_RECORDING,
            "running": self.is_running,
            "transfer_mode": settings.recording_transfer_mode,
            "destination_folder": settings.RECORDING_DRIVE_FOLDER_NAME,
            "share_with_students": settings.RECORDING_SHARE_WITH_STUDENTS,
            "harvest_delay_minutes": settings.RECORDING_HARVEST_DELAY_MINUTES,
            "scan_interval_seconds": settings.RECORDING_SCAN_INTERVAL_SECONDS,
            "give_up_after_hours": settings.RECORDING_MAX_AGE_HOURS,
            "drive_configured": google_drive.is_configured(),
            "meet_problems": meet_recordings.configuration_problems(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "run_count": self.run_count,
            "last_error": self.last_error,
            "last_result": self.last_result,
        }


def get_scheduler() -> RecordingScheduler:
    """The process-wide recording scheduler, created on first use."""
    global _scheduler
    if _scheduler is None:
        _scheduler = RecordingScheduler()
    return _scheduler
