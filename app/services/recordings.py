"""
Class recordings: started with the teacher's call, filed into Blob storage when ACS is done.

The lifecycle of one recording, as a `recording_log` document keyed by ACS's recording id:

    RECORDING    started when the session's teacher connected (app/services/calls.py)
    STOPPED      the teacher hung up, or RECORDING_MAX_MINUTES passed; ACS is writing the file
    FILE_READY   ACS announced the file through Event Grid; it is being copied into Blob
    STORED       the video is in Blob storage and on the session as `recording_url`
    FAILED       it could not be copied, or ACS never announced it; `error` says why

And the lifecycle written onto each session as `recording_status`, which the screens read:

    NOT_REQUESTED  recording was off for this session
    ARMED          it will be recorded when its teacher joins
    RECORDING      it is being recorded now
    WAITING        the recording has stopped and the file is on its way
    STORED         the video is filed and `recording_url` points at it
    UNAVAILABLE    nothing was ever recorded; given up after RECORDING_MAX_AGE_HOURS
    FAILED         a recording exists but could not be filed; `recording_error` says why

Three properties matter, because an automated file mover fails loudly or not at all:

  * **Exactly once.** A file is claimed per (recording, chunk) with an atomic create before it
    is copied, so a webhook retried by Event Grid and the sweep racing it never file twice.
  * **Nothing is lost to a slow request.** The webhook only records what arrived and answers
    at once; the copy runs in the background, and the sweep retries any copy that did not
    finish, for as long as ACS keeps the file.
  * **Nothing polls for ever.** A session nobody recorded is marked UNAVAILABLE once
    RECORDING_MAX_AGE_HOURS have passed since it ended.
"""

import hashlib
import logging
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from app.core import acs, azure_blob
from app.core.config import settings
from app.core.firebase import firestore_meetings, firestore_recording_log, log_backend_failure

logger = logging.getLogger("recordings")

STATUS_NOT_REQUESTED = "NOT_REQUESTED"
STATUS_ARMED = "ARMED"
STATUS_RECORDING = "RECORDING"
STATUS_WAITING = "WAITING"
STATUS_STORED = "STORED"
STATUS_UNAVAILABLE = "UNAVAILABLE"
STATUS_FAILED = "FAILED"

LOG_RECORDING = "RECORDING"
LOG_STOPPED = "STOPPED"
LOG_FILE_READY = "FILE_READY"
LOG_STORED = "STORED"
LOG_FAILED = "FAILED"
LOG_UNMATCHED = "UNMATCHED"

# How many times the sweep retries copying a file before calling it FAILED.
MAX_COPY_ATTEMPTS = 5

_scheduler: "RecordingScheduler | None" = None


def _now() -> datetime:
    return datetime.utcnow()


def _parse_time(value) -> datetime | None:
    """Any stored or ACS timestamp, as naive UTC."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    # ACS writes seven fractional digits; fromisoformat takes at most six.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = "".join(ch for ch in tail if ch.isdigit())
        rest = tail[len(digits):]
        text = f"{head}.{digits[:6]}{rest}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def meeting_ended_at(meeting: dict) -> datetime | None:
    """When a session was scheduled to finish, in the same naive-UTC frame as created_at."""
    started = _parse_time(meeting.get("scheduled_time"))
    if started is None:
        return None
    return started + timedelta(minutes=max(int(meeting.get("duration_minutes") or 60), 1))


def log_key(recording_id: str) -> str:
    """
    The `recording_log` document id for an ACS recording. ACS recording ids are long base64
    strings - longer than a document id may be in Azure SQL - so the log is keyed by a hash
    of it and the full id is kept inside the document.
    """
    return "rec_" + hashlib.sha256(recording_id.encode("utf-8")).hexdigest()[:40]


def _copy_claim_key(recording_id: str) -> str:
    return "copy_" + log_key(recording_id)


def _update_meeting(meeting_id, **fields) -> None:
    if meeting_id is not None:
        firestore_meetings.add_document(str(meeting_id), fields)


def configuration_problems() -> list[str]:
    return acs.configuration_problems() + acs.recording_problems()


# ---------------------------------------------------------------------------------------
# Starting and stopping (called by the call page's connect and hang-up)
# ---------------------------------------------------------------------------------------

def active_for(entity_key: str) -> list[dict]:
    return [
        r for r in firestore_recording_log.query_documents("entity_key", "==", entity_key)
        if r.get("status") == LOG_RECORDING
    ]


def start(*, kind: str, entity_id, room_id: str, server_call_id: str | None, user,
          title: str, class_id=None, meeting_id=None, subject_id=None) -> dict | None:
    """
    Starts recording a live call, unless this entity's call is already being recorded.

    Returns the log document, or None when nothing was started. Never raises: a class that
    cannot be recorded must still be taught.
    """
    entity_key = f"{kind}:{entity_id}"
    if active_for(entity_key):
        return None

    try:
        recording_id = acs.start_recording(server_call_id=server_call_id, room_id=room_id)
    except acs.AcsError as exc:
        logger.warning("Recording of %s not started: %s", entity_key, exc)
        _update_meeting(meeting_id, recording_status=STATUS_FAILED, recording_error=str(exc))
        return None

    now = _now().isoformat()
    record = {
        "recording_id": recording_id,
        # `recording_name` and `claimed_at` are what the admin's log screen has always read.
        "recording_name": recording_id,
        "claimed_at": now,
        "entity_key": entity_key,
        "kind": kind,
        "entity_id": str(entity_id),
        "class_id": int(class_id) if class_id is not None else None,
        "meeting_id": int(meeting_id) if meeting_id is not None else None,
        "subject_id": int(subject_id) if subject_id is not None else None,
        "title": title,
        "room_id": room_id,
        "server_call_id": server_call_id,
        "started_by": int(user.id),
        "started_by_name": getattr(user, "full_name", None),
        "started_at": now,
        "status": LOG_RECORDING,
        "files": [],
        # The teacher reviews a finished recording before students see it; publishing files
        # it into the class library (see "Publishing" below).
        "published": False,
    }
    firestore_recording_log.add_document(log_key(recording_id), record)
    _update_meeting(meeting_id, recording_status=STATUS_RECORDING, recording_error=None)
    logger.info("Recording %s started for %s by user %s.", recording_id, entity_key, user.id)
    return record


def stop(record: dict, reason: str = "The teacher left the class.") -> bool:
    """Stops one recording and marks it STOPPED. Safe to call twice."""
    if record.get("status") != LOG_RECORDING:
        return True
    stopped = acs.stop_recording(record["recording_id"])
    firestore_recording_log.add_document(log_key(record["recording_id"]), {
        "status": LOG_STOPPED if stopped else LOG_RECORDING,
        "stopped_at": _now().isoformat() if stopped else None,
        "stop_reason": reason,
    })
    if stopped:
        _update_meeting(record.get("meeting_id"), recording_status=STATUS_WAITING)
    return stopped


def stop_for(entity_key: str, user=None, reason: str = "The teacher left the class.") -> int:
    """Stops this entity's running recordings - only those `user` started, when given."""
    count = 0
    for record in active_for(entity_key):
        if user is not None and record.get("started_by") not in (None, int(user.id)):
            continue
        if stop(record, reason):
            count += 1
    return count


# ---------------------------------------------------------------------------------------
# The file: Event Grid tells us it is ready, and we copy it into Blob storage
# ---------------------------------------------------------------------------------------

def _server_call_id_from(subject: str | None) -> str | None:
    """`/recording/call/{callId}/serverCallId/{serverCallId}` -> the server call id."""
    parts = [p for p in str(subject or "").split("/") if p]
    for index, part in enumerate(parts):
        if part.lower() == "servercallid" and index + 1 < len(parts):
            return parts[index + 1]
    return None


def _match(server_call_id: str | None, started: datetime | None, recording_id: str | None) -> dict | None:
    """Which recording an announced file belongs to."""
    if recording_id:
        record = firestore_recording_log.get_document(log_key(recording_id))
        if record:
            return record
    if not server_call_id:
        return None
    # STORED too: Event Grid redelivers, and a long recording can be announced again with
    # further chunks. `handle_file_ready` tells the two apart.
    candidates = [
        r for r in firestore_recording_log.query_documents("server_call_id", "==", server_call_id)
        if r.get("status") in (LOG_RECORDING, LOG_STOPPED, LOG_FILE_READY, LOG_STORED)
    ]
    if not candidates:
        return None
    if started is None or len(candidates) == 1:
        return candidates[0]
    # Several periods recorded in one day-long class call: the file is the one whose start
    # is nearest the moment we asked ACS to begin.
    return min(candidates, key=lambda r: abs(((_parse_time(r.get("started_at")) or started) - started).total_seconds()))


def handle_file_ready(event: dict) -> dict | None:
    """
    Records an Event Grid RecordingFileStatusUpdated event against its recording.

    Returns the updated log document (with its pending chunks) for the caller to copy, or
    None when the event matched nothing we started - a recording made from another app on
    the same resource, which is kept as UNMATCHED so it can be looked at.
    """
    data = event.get("data") or {}
    chunks = ((data.get("recordingStorageInfo") or {}).get("recordingChunks")) or []
    server_call_id = _server_call_id_from(event.get("subject"))
    started = _parse_time(data.get("recordingStartTime"))
    record = _match(server_call_id, started, data.get("recordingId"))

    pending = [
        {
            "document_id": c.get("documentId"),
            "index": c.get("index", 0),
            "content_location": c.get("contentLocation"),
            "delete_location": c.get("deleteLocation"),
        }
        for c in chunks if c.get("contentLocation")
    ]

    if record is None:
        key = f"unmatched_{uuid.uuid4().hex[:12]}"
        firestore_recording_log.add_document(key, {
            "recording_name": key,
            "claimed_at": _now().isoformat(),
            "status": LOG_UNMATCHED,
            "server_call_id": server_call_id,
            "recording_start_time": data.get("recordingStartTime"),
            "pending_chunks": pending,
            "error": "ACS announced a recording the LMS did not start.",
        })
        logger.warning("Unmatched recording file for server call %s.", server_call_id)
        return None

    filed = {f.get("document_id") for f in record.get("files") or []}
    if pending and all(chunk["document_id"] in filed for chunk in pending):
        # A redelivery of an event already handled.
        return None

    updates = {
        "status": LOG_FILE_READY,
        "file_ready_at": _now().isoformat(),
        "recording_start_time": data.get("recordingStartTime"),
        "recording_duration_ms": data.get("recordingDurationMs"),
        "pending_chunks": pending,
        "copy_attempts": 0,
    }
    firestore_recording_log.add_document(log_key(record["recording_id"]), updates)
    return {**record, **updates}


def _filename(record: dict, index: int) -> str:
    started = _parse_time(record.get("recording_start_time") or record.get("started_at")) or _now()
    title = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in (record.get("title") or "class")).strip()
    suffix = f"-part{index + 1}" if index else ""
    # The random part keeps the address unguessable: anyone holding it may watch the video.
    return f"{started:%Y-%m-%d %H%M} {title[:60]}{suffix} {uuid.uuid4().hex[:8]}.mp4"


def copy_to_storage(record: dict) -> dict:
    """
    Copies every announced chunk of one recording into Blob storage and files it on the
    session. Returns {"status", "detail", "files"}. Never raises.
    """
    from app.core.storage import PROVIDER_BLOB, storage_service

    recording_id = record["recording_id"]
    claim_key = _copy_claim_key(recording_id)
    if not firestore_recording_log.create_document(claim_key, {"claimed_at": _now().isoformat()}):
        # Another worker, or an earlier attempt that died mid-copy. The sweep releases stale
        # claims (see `_retry_copies`), so this is never permanent.
        return {"status": "BUSY", "detail": "Another worker is copying this recording.", "files": []}

    folder_class = f"class_{record.get('class_id')}" if record.get("class_id") is not None else record.get("kind") or "other"
    folder = f"{settings.RECORDING_FOLDER_NAME}/{folder_class}"
    files, errors = list(record.get("files") or []), []
    done = {f.get("document_id") for f in files}

    try:
        for chunk in sorted(record.get("pending_chunks") or [], key=lambda c: c.get("index") or 0):
            if chunk.get("document_id") in done:
                continue
            try:
                with tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024) as buffer:
                    size = 0
                    for piece in acs.download_recording(chunk["content_location"]):
                        buffer.write(piece)
                        size += len(piece)
                    buffer.seek(0)
                    stored = storage_service.save_stream(
                        buffer, _filename(record, int(chunk.get("index") or 0)), folder,
                        content_type="video/mp4", unique=False,
                    )
                if stored["provider"] != PROVIDER_BLOB:
                    errors.append(stored.get("warning") or "The recording landed on local disk, not Blob storage.")
                files.append({
                    "document_id": chunk.get("document_id"),
                    "file_url": stored["url"],
                    # The names the screens have read since the Drive era.
                    "drive_file_id": stored.get("file_id"),
                    "web_view_link": stored["url"],
                    "name": stored["url"].rsplit("/", 1)[-1],
                    "size_bytes": size,
                    "started_at": record.get("recording_start_time"),
                    "ended_at": None,
                })
            except Exception as exc:
                errors.append(f"Chunk {chunk.get('index')}: {exc}")
                logger.warning("Recording %s chunk %s not copied: %s", recording_id, chunk.get("index"), exc)
    finally:
        firestore_recording_log.delete_document(claim_key)

    attempts = int(record.get("copy_attempts") or 0) + 1
    complete = files and len(files) >= len(record.get("pending_chunks") or [])
    if complete:
        primary = files[0]
        firestore_recording_log.add_document(log_key(recording_id), {
            "status": LOG_STORED, "files": files, "finished_at": _now().isoformat(),
            "file_url": primary["file_url"], "web_view_link": primary["file_url"],
            "drive_file_id": primary["drive_file_id"], "mode": "AZURE_BLOB",
            "error": None, "warning": " | ".join(errors) or None, "copy_attempts": attempts,
        })
        _file_on_session(record, files, " | ".join(errors) or None)
        _auto_publish(log_key(recording_id))
        return {"status": STATUS_STORED, "detail": f"{len(files)} file(s) filed.", "files": files}

    final = attempts >= MAX_COPY_ATTEMPTS
    detail = " | ".join(errors) or "The recording could not be copied."
    firestore_recording_log.add_document(log_key(recording_id), {
        "status": LOG_FAILED if final else LOG_FILE_READY,
        "files": files, "error": detail, "copy_attempts": attempts,
    })
    if final:
        _update_meeting(record.get("meeting_id"), recording_status=STATUS_FAILED, recording_error=detail)
    return {"status": STATUS_FAILED if final else STATUS_WAITING, "detail": detail, "files": files}


def _file_on_session(record: dict, files: list[dict], warning: str | None) -> None:
    """Puts a stored recording on its session, adding to any the session already has."""
    meeting_id = record.get("meeting_id")
    if meeting_id is None:
        return
    meeting = firestore_meetings.get_document(str(meeting_id)) or {}
    existing = list(meeting.get("recording_files") or [])
    known = {f.get("file_url") or f.get("web_view_link") for f in existing}
    merged = existing + [f for f in files if f["file_url"] not in known]
    _update_meeting(
        meeting_id,
        recording_status=STATUS_STORED,
        recording_url=meeting.get("recording_url") if meeting.get("recording_status") == STATUS_STORED and meeting.get("recording_url") else merged[0]["file_url"],
        recording_drive_file_id=merged[0].get("drive_file_id"),
        recording_transfer_mode="AZURE_BLOB",
        recording_stored_at=_now().isoformat(),
        recording_error=warning,
        recording_files=merged,
    )


# ---------------------------------------------------------------------------------------
# Publishing: from the teacher's review into the class library
#
# A finished recording belongs to the teacher who taught it until they publish it. Publishing
# files each part as a study material of type RECORDING, under the recording's class and
# subject - so it appears in that class's library, and only students of that class can open
# it (recordings are private files, signed per viewer). A teacher may instead set their own
# rule to publish automatically (`recording_auto_publish` on their profile).
# ---------------------------------------------------------------------------------------

MATERIAL_TYPE_RECORDING = "RECORDING"


def get_record(key: str) -> dict | None:
    record = firestore_recording_log.get_document(key)
    return record if record and record.get("recording_id") else None


def may_manage(record: dict, user) -> bool:
    """The teacher who recorded it, the class teacher of its class, or an admin."""
    from app.core.enums import UserRole
    from app.services import permissions

    if getattr(user, "role", None) == UserRole.ADMIN:
        return True
    if record.get("started_by") is not None and int(record["started_by"]) == int(user.id):
        return True
    class_id = record.get("class_id")
    return class_id is not None and permissions.is_class_teacher_of(user, class_id)


def default_title(record: dict, subject_name: str | None = None) -> str:
    started = _parse_time(record.get("recording_start_time") or record.get("started_at")) or _now()
    base = subject_name or record.get("title") or "Class recording"
    return f"{base} - {started:%d %b %Y}"


def publish(record: dict, actor_id: int | None = None, title: str | None = None,
            subject_id: int | None = None) -> dict:
    """
    Files a stored recording into its class library. Raises ValueError with a reason the
    teacher can act on (not stored yet, no class, no subject). Idempotent: publishing again
    only updates the title.
    """
    from app.core.firebase import firestore_materials, firestore_subjects

    if record.get("status") != LOG_STORED or not record.get("files"):
        raise ValueError("This recording is still being prepared. Publish it once it is ready.")
    class_id = record.get("class_id")
    if class_id is None:
        raise ValueError("This recording has no class to publish it to.")
    subject_id = subject_id if subject_id is not None else record.get("subject_id")
    if subject_id is None:
        raise ValueError("Choose the subject this recording belongs to.")
    subject = firestore_subjects.get_document(str(subject_id))
    if not subject:
        raise ValueError(f"Subject {subject_id} not found.")

    key = log_key(record["recording_id"])
    title = (title or "").strip() or default_title(record, subject.get("name"))
    files = record["files"]
    existing = list(record.get("material_ids") or [])
    material_ids = []
    for index, file in enumerate(files):
        part_title = title if len(files) == 1 else f"{title} (part {index + 1})"
        material_id = existing[index] if index < len(existing) else firestore_materials.get_next_numeric_id()
        firestore_materials.add_document(str(material_id), {
            "class_id": int(class_id),
            "subject_id": int(subject_id),
            "teacher_id": int(record.get("started_by") or actor_id or 0),
            "title": part_title,
            "material_type": MATERIAL_TYPE_RECORDING,
            "file_url": file["file_url"],
            "storage_provider": "AZURE_BLOB",
            "storage_warning": None,
            "uploaded_at": record.get("finished_at") or _now().isoformat(),
            "recording_key": key,
            "size_bytes": file.get("size_bytes"),
            "duration_ms": record.get("recording_duration_ms"),
        })
        material_ids.append(int(material_id))

    updates = {
        "published": True,
        "published_at": _now().isoformat(),
        "published_by": actor_id,
        "published_title": title,
        "subject_id": int(subject_id),
        "material_ids": material_ids,
    }
    firestore_recording_log.add_document(key, updates)
    _update_meeting(record.get("meeting_id"), recording_published=True)
    logger.info("Recording %s published to class %s as %d material(s).", key, class_id, len(material_ids))
    return {**record, **updates}


def unpublish(record: dict) -> dict:
    """Takes a recording back out of the library. The video itself is kept."""
    from app.core.firebase import firestore_materials

    for material_id in record.get("material_ids") or []:
        firestore_materials.delete_document(str(material_id))
    updates = {"published": False, "material_ids": [], "published_at": None}
    firestore_recording_log.add_document(log_key(record["recording_id"]), updates)
    _update_meeting(record.get("meeting_id"), recording_published=False)
    return {**record, **updates}


def material_removed(material: dict) -> None:
    """
    A RECORDING material was deleted from the library screens: the recording behind it is
    unpublished rather than its video deleted, so the teacher can still find and republish it.
    """
    record = get_record(material.get("recording_key") or "")
    if record is None:
        return
    remaining = [m for m in record.get("material_ids") or [] if int(m) != int(material["id"])]
    if remaining:
        firestore_recording_log.add_document(log_key(record["recording_id"]), {"material_ids": remaining})
    else:
        unpublish({**record, "material_ids": []})


def _auto_publish(key: str) -> None:
    """The teacher's own rule: publish as soon as the video is stored."""
    record = get_record(key)
    if not record or record.get("published") or record.get("started_by") is None:
        return
    from app.core.firebase import firestore_users

    teacher = firestore_users.get_document(str(record["started_by"])) or {}
    if not teacher.get("recording_auto_publish"):
        return
    try:
        publish(record, actor_id=int(record["started_by"]))
    except ValueError as exc:
        # No subject on a period-less recording, typically: it waits for the teacher.
        logger.info("Recording %s not auto-published: %s", key, exc)


def present(record: dict) -> dict:
    """A recording as its teacher's review screen shows it, with signed links to watch."""
    from app.core.azure_blob import sign_private
    from app.core.firebase import firestore_classes, firestore_subjects

    status = record.get("status")
    shown_status = {
        LOG_RECORDING: STATUS_RECORDING, LOG_STOPPED: STATUS_WAITING, LOG_FILE_READY: STATUS_WAITING,
        LOG_STORED: STATUS_STORED, LOG_FAILED: STATUS_FAILED,
    }.get(status, status)
    class_room = firestore_classes.get_document(str(record.get("class_id"))) if record.get("class_id") is not None else None
    subject = firestore_subjects.get_document(str(record.get("subject_id"))) if record.get("subject_id") is not None else None
    return {
        "key": log_key(record["recording_id"]),
        "title": record.get("published_title") or record.get("title"),
        "default_title": default_title(record, (subject or {}).get("name")),
        "status": shown_status,
        "error": record.get("error"),
        "class_id": record.get("class_id"),
        "class_name": (class_room or {}).get("name"),
        "subject_id": record.get("subject_id"),
        "subject_name": (subject or {}).get("name"),
        "meeting_id": record.get("meeting_id"),
        "started_by": record.get("started_by"),
        "started_by_name": record.get("started_by_name"),
        "started_at": record.get("started_at"),
        "stopped_at": record.get("stopped_at"),
        "duration_ms": record.get("recording_duration_ms"),
        "published": bool(record.get("published")),
        "published_at": record.get("published_at"),
        "material_ids": record.get("material_ids") or [],
        "files": [
            {**f, "file_url": sign_private(f.get("file_url")), "web_view_link": sign_private(f.get("web_view_link"))}
            for f in record.get("files") or []
        ],
    }


def recordings_for(user) -> list[dict]:
    """The recordings a teacher reviews: their own, plus their led classes'; all for an admin."""
    records = [
        r for r in firestore_recording_log.list_all()
        if r.get("recording_id") and r.get("status") != LOG_UNMATCHED and may_manage(r, user)
    ]
    records.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
    return records


def process_event_async(event: dict) -> None:
    """The webhook's background half: match the event, then copy the file."""
    try:
        record = handle_file_ready(event)
        if record is not None:
            copy_to_storage(record)
    except Exception as exc:
        log_backend_failure(logger, "Recording event could not be processed", exc)


# ---------------------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------------------

def _retry_copies(summary: dict, dry_run: bool) -> None:
    for record in firestore_recording_log.query_documents("status", "==", LOG_FILE_READY):
        if dry_run:
            summary["details"].append({"meeting_id": record.get("meeting_id"), "title": record.get("title"),
                                       "status": "WOULD_STORE", "detail": "File announced; copy pending.",
                                       "transferred": len(record.get("pending_chunks") or [])})
            summary["stored"] += 1
            continue
        # A copy claim older than an hour belongs to a worker that died mid-copy.
        claim = firestore_recording_log.get_document(_copy_claim_key(record["recording_id"]))
        if claim and (_parse_time(claim.get("claimed_at")) or _now()) < _now() - timedelta(hours=1):
            firestore_recording_log.delete_document(_copy_claim_key(record["recording_id"]))
        result = copy_to_storage(record)
        summary["details"].append({"meeting_id": record.get("meeting_id"), "title": record.get("title"),
                                   "status": result["status"], "detail": result["detail"],
                                   "transferred": len(result["files"])})
        if result["status"] == STATUS_STORED:
            summary["stored"] += len(result["files"])
        elif result["status"] == STATUS_FAILED:
            summary["failed"] += 1
        else:
            summary["waiting"] += 1


def _close_stale_recordings(summary: dict, dry_run: bool) -> None:
    now = _now()
    cap = timedelta(minutes=max(int(settings.RECORDING_MAX_MINUTES), 10))
    give_up = timedelta(hours=max(int(settings.RECORDING_MAX_AGE_HOURS), 1))
    for record in firestore_recording_log.query_documents("status", "==", LOG_RECORDING):
        started = _parse_time(record.get("started_at"))
        if started and now - started > cap and not dry_run:
            stop(record, reason=f"Stopped after {settings.RECORDING_MAX_MINUTES} minutes.")
    for record in firestore_recording_log.query_documents("status", "==", LOG_STOPPED):
        stopped = _parse_time(record.get("stopped_at") or record.get("started_at"))
        if stopped and now - stopped > give_up:
            detail = (
                "ACS never announced this recording's file. Check the Event Grid subscription "
                "for RecordingFileStatusUpdated on the Communication resource."
            )
            summary["failed"] += 1
            summary["details"].append({"meeting_id": record.get("meeting_id"), "title": record.get("title"),
                                       "status": STATUS_FAILED, "detail": detail, "transferred": 0})
            if not dry_run:
                firestore_recording_log.add_document(log_key(record["recording_id"]), {"status": LOG_FAILED, "error": detail})
                _update_meeting(record.get("meeting_id"), recording_status=STATUS_FAILED, recording_error=detail)


def _give_up_on_unrecorded(summary: dict, reference: datetime, dry_run: bool) -> None:
    """Sessions set to record whose teacher never joined: marked UNAVAILABLE, once."""
    give_up = timedelta(hours=max(int(settings.RECORDING_MAX_AGE_HOURS), 1))
    for meeting in firestore_meetings.list_all():
        if meeting.get("recording_status") not in (STATUS_ARMED, "ARM_FAILED"):
            continue
        ended = meeting_ended_at(meeting)
        if ended is None or reference - ended < give_up:
            continue
        detail = "Nobody started this session's recording: its teacher did not join through the LMS."
        summary["unavailable"] += 1
        summary["details"].append({"meeting_id": meeting.get("id"), "title": meeting.get("title"),
                                   "status": STATUS_UNAVAILABLE, "detail": detail, "transferred": 0})
        if not dry_run:
            _update_meeting(meeting["id"], recording_status=STATUS_UNAVAILABLE, recording_error=detail)


def sweep(
    reference: datetime | None = None,
    dry_run: bool = False,
    progress: Callable[[int, str], None] | None = None,
) -> dict:
    """
    Retries unfinished copies, stops recordings nobody stopped, and closes out sessions that
    were never recorded. Returns a job-shaped summary. `dry_run` changes nothing.
    """
    def report(percent: int, message: str) -> None:
        if progress:
            progress(percent, message)

    reference = reference or _now()
    summary = {
        "due_meetings": 0,
        "stored": 0,
        "waiting": 0,
        "unavailable": 0,
        "failed": 0,
        "dry_run": dry_run,
        "transfer_mode": "AZURE_BLOB",
        "reference": reference.isoformat(),
        "details": [],
    }

    problems = configuration_problems()
    if problems and not dry_run:
        summary["detail"] = " ".join(problems)
        return summary

    report(10, "Copying announced recordings")
    _retry_copies(summary, dry_run)
    report(50, "Closing stale recordings")
    _close_stale_recordings(summary, dry_run)
    report(80, "Closing sessions nobody recorded")
    _give_up_on_unrecorded(summary, reference, dry_run)

    summary["due_meetings"] = len(summary["details"])
    summary["detail"] = (
        f"{summary['stored']} recording(s) filed, {summary['waiting']} still copying, "
        f"{summary['unavailable']} session(s) never recorded, {summary['failed']} failed."
    )
    if problems:
        summary["detail"] += " " + " ".join(problems)
    return summary


def harvest_meeting(meeting: dict, dry_run: bool = False) -> dict:
    """
    Retries filing one session's announced recordings now, for the session somebody is
    asking about. Returns {"meeting_id", "title", "status", "detail", "recording_url",
    "transferred"}.
    """
    meeting_id = meeting.get("id")
    summary = {
        "meeting_id": meeting_id,
        "title": meeting.get("title"),
        "status": meeting.get("recording_status"),
        "detail": "",
        "recording_url": meeting.get("recording_url"),
        "transferred": 0,
    }
    records = firestore_recording_log.query_documents("meeting_id", "==", int(meeting_id)) if meeting_id is not None else []
    ready = [r for r in records if r.get("status") == LOG_FILE_READY]
    if not ready:
        running = [r for r in records if r.get("status") in (LOG_RECORDING, LOG_STOPPED)]
        summary["detail"] = (
            "The recording is still running or being written by ACS." if running else
            "No recording is waiting to be filed for this session."
        )
        return summary
    if dry_run:
        return {**summary, "status": "WOULD_STORE", "transferred": len(ready)}
    for record in ready:
        result = copy_to_storage(record)
        summary["status"], summary["detail"] = result["status"], result["detail"]
        summary["transferred"] += len(result["files"])
    refreshed = firestore_meetings.get_document(str(meeting_id)) or {}
    summary["recording_url"] = refreshed.get("recording_url")
    return summary


class RecordingScheduler:
    """
    Daemon thread that calls `sweep()` on an interval.

    Each server worker runs its own; the per-recording copy claim makes a duplicate file
    impossible - a second worker finds the copy claimed and moves on.
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
        if not settings.ENABLE_AUTO_RECORDING:
            logger.info("Automatic class recording is disabled (ENABLE_AUTO_RECORDING=False).")
            return False

        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="lms-recordings", daemon=True)
        self._thread.start()
        self.started_at = _now()
        logger.info("Class recording sweep started: every %.0fs.", settings.RECORDING_SCAN_INTERVAL_SECONDS)
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
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
                log_backend_failure(logger, "Recording sweep failed", exc)
            finally:
                self.last_run_at = _now()
                self.run_count += 1

            self._stop.wait(interval)

    def status(self) -> dict:
        return {
            "enabled": settings.ENABLE_AUTO_RECORDING,
            "running": self.is_running,
            "transfer_mode": "AZURE_BLOB",
            "destination_folder": settings.RECORDING_FOLDER_NAME,
            "share_with_students": True,
            "harvest_delay_minutes": 0,
            "scan_interval_seconds": settings.RECORDING_SCAN_INTERVAL_SECONDS,
            "give_up_after_hours": settings.RECORDING_MAX_AGE_HOURS,
            "max_recording_minutes": settings.RECORDING_MAX_MINUTES,
            "storage_configured": azure_blob.is_configured(),
            # Names kept for the admin screen, which has read them since the Meet era.
            "drive_configured": azure_blob.is_configured(),
            "meet_problems": configuration_problems(),
            "problems": configuration_problems(),
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
