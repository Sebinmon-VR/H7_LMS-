"""
Meeting scheduling and study-material storage, shared by the teacher and admin routers.

Both roles perform the same two actions - schedule a live session with a live-class room,
and upload a file to the configured storage backend - and the only difference is *whose*
name the record is filed under. Keeping the logic here means an admin acting on behalf of a
teacher gets byte-identical behaviour instead of a second, subtly divergent implementation.
"""

import logging
from datetime import datetime

from fastapi import HTTPException, UploadFile

from app.core import acs
from app.core.config import settings
from app.core.enums import TEACHING_OR_ADMIN_VALUES
from app.core.storage import StorageError, storage_service
from app.core.firebase import (
    firestore_classes, firestore_materials, firestore_meetings, firestore_subjects,
    firestore_student_enrollments, firestore_users,
)
from app.schemas.meeting import LiveMeetingCreate
from app.services.links import LINK_KIND_YOUTUBE, parse_link

logger = logging.getLogger("content_service")

# Room outcomes recorded on every meeting as `meet_status` (named for the Meet era).
MEET_MANUAL = "MANUAL"     # A link was supplied by hand; nothing was generated.
MEET_SKIPPED = "SKIPPED"   # A room was not requested.
MEET_CREATED = "CREATED"   # The session has an LMS room of its own.
MEET_FAILED = "FAILED"     # A room was asked for and could not be given.

# Where a session starts in the recording lifecycle. The rest of it - WAITING, STORED,
# UNAVAILABLE, FAILED - is driven by the sweep in app/services/recordings.py.
RECORDING_NOT_REQUESTED = "NOT_REQUESTED"
RECORDING_ARMED = "ARMED"


def active_students_in_class(class_id: int) -> list[dict]:
    """
    Returns the active student documents enrolled in a class.
    Resolves every student in one batched read rather than one call per enrollment.
    """
    enrollments = firestore_student_enrollments.query_documents("class_id", "==", class_id)
    if not enrollments:
        return []

    student_ids = [e.get("student_id") for e in enrollments if e.get("student_id") is not None]
    resolved = firestore_users.get_documents(student_ids)

    return [
        student for student in
        (resolved.get(str(sid)) for sid in student_ids)
        if student and student.get("is_active", False)
    ]


def resolve_teacher(teacher_id: int) -> dict:
    """
    Loads the teacher a record is being filed under, for the admin-on-behalf-of flows.
    Admins are accepted too, so an admin can own a session they run themselves.
    """
    teacher = firestore_users.get_document(str(teacher_id))
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")

    if teacher.get("role") not in TEACHING_OR_ADMIN_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"User {teacher_id} is not a teacher and cannot own this record.",
        )
    return teacher


def assert_class_and_subject_exist(class_id: int, subject_id: int) -> None:
    """
    Rejects records pointing at a class or subject that does not exist.

    Without this an upload succeeds and then hydrates to a row with empty class and subject
    columns, which reads as "the upload broke" long after the real mistake was made.
    """
    if not firestore_classes.get_document(str(class_id)):
        raise HTTPException(status_code=404, detail=f"ClassRoom {class_id} not found")
    if not firestore_subjects.get_document(str(subject_id)):
        raise HTTPException(status_code=404, detail=f"Subject {subject_id} not found")


def schedule_meeting(
    meeting_in: LiveMeetingCreate,
    teacher_id: int,
    teacher_email: str,
) -> dict:
    """
    Creates a live session and, when asked, gives it a live-class room in the LMS.

    Returns the persisted meeting document (including its `id`). The meeting is always
    saved: if no room can be given, `meet_status` is FAILED and `meet_error` carries the
    reason, so the schedule survives and the cause is visible in the API response instead of
    only in the server log.

    A session of its own gets the link /call/meeting/<id> straight away; the ACS room behind
    it is made the first time somebody is let in (app/services/calls.py), so a session that
    is rescheduled or cancelled before it happens never costs a room.
    """
    assert_class_and_subject_exist(meeting_in.class_id, meeting_in.subject_id)

    meeting_id = firestore_meetings.get_next_numeric_id()
    meeting_link = meeting_in.meeting_link
    meet_error = None
    uses_class_room = False
    auto_record = meeting_in.auto_record
    # A hand-entered link belongs to a conference the LMS does not own, so there is nothing
    # to record and nothing to collect afterwards.
    recording_status = RECORDING_NOT_REQUESTED
    recording_error = None

    # Imported here: class_rooms reads the meetings collection through live_classes, and a
    # top-level import either way would be circular.
    from app.services import class_rooms

    if meeting_link:
        meet_status = MEET_MANUAL
    elif class_rooms.class_room_mode():
        # The classroom model: this period joins the class's one standing room rather than
        # having a room of its own. Created on first need, so the first period a class ever
        # schedules is what brings its room into being.
        class_room = firestore_classes.get_document(str(meeting_in.class_id)) or {}
        class_room, room_error = class_rooms.ensure_room(
            class_room, actor_id=teacher_id, actor_email=teacher_email
        )
        if class_rooms.has_room(class_room):
            room_fields = class_rooms.meeting_fields_for_room(class_room)
            meeting_link = room_fields["meeting_link"]
            meet_status = room_fields["meet_status"]
            auto_record = room_fields["auto_record"]
            recording_status = room_fields["recording_status"]
            recording_error = room_fields["recording_error"]
            uses_class_room = True
        else:
            meet_status = MEET_FAILED
            meet_error = room_error
    elif not meeting_in.auto_create_meet:
        meet_status = MEET_SKIPPED
    elif not acs.is_enabled():
        meet_status = MEET_FAILED
        meet_error = " ".join(acs.configuration_problems()) or "Live classes are not configured."
    else:
        meeting_link = acs.call_link(acs.KIND_MEETING, meeting_id)
        meet_status = MEET_CREATED
        if auto_record and settings.ENABLE_AUTO_RECORDING:
            recording_status = RECORDING_ARMED

    meeting_data = {
        "class_id": meeting_in.class_id,
        "subject_id": meeting_in.subject_id,
        "teacher_id": teacher_id,
        "title": meeting_in.title,
        "meeting_link": meeting_link,
        "recording_url": meeting_in.recording_url,
        "scheduled_time": meeting_in.scheduled_time.isoformat(),
        "status": meeting_in.status,
        "duration_minutes": meeting_in.duration_minutes,
        "meet_status": meet_status,
        "meet_error": meet_error,
        "auto_record": auto_record,
        "recording_status": recording_status,
        "recording_error": recording_error,
        # Set when the session uses the class's shared room rather than one of its own.
        "uses_class_room": uses_class_room,
        # The ACS room of a session with its own; made on first join.
        "acs_room_id": None,
        "created_at": datetime.utcnow().isoformat(),
    }
    firestore_meetings.add_document(str(meeting_id), meeting_data)
    meeting_data["id"] = meeting_id
    return meeting_data


def is_legacy_meet_link(link: str | None) -> bool:
    """A Google Meet link from before the move to Azure. They stopped working with the Workspace."""
    return "meet.google.com" in str(link or "").lower()


def regenerate_meeting_link(meeting: dict) -> dict:
    """
    Gives a session a fresh LMS room: its old ACS room is dropped and the link points at the
    session again. Returns the fields written. A class-room session is re-pointed at its
    class's room instead.
    """
    from app.services import class_rooms

    acs.delete_room(meeting.get("acs_room_id"))
    if meeting.get("uses_class_room") and meeting.get("class_id") is not None:
        class_room = firestore_classes.get_document(str(meeting["class_id"])) or {}
        class_room, error = class_rooms.ensure_room(class_room)
        if class_rooms.has_room(class_room):
            updates = {**class_rooms.meeting_fields_for_room(class_room), "acs_room_id": None}
        else:
            updates = {"meet_status": MEET_FAILED, "meet_error": error}
    elif not acs.is_enabled():
        updates = {
            "meet_status": MEET_FAILED,
            "meet_error": " ".join(acs.configuration_problems()) or "Live classes are not configured.",
        }
    else:
        updates = {
            "meeting_link": acs.call_link(acs.KIND_MEETING, meeting["id"]),
            "meet_status": MEET_CREATED,
            "meet_error": None,
            "acs_room_id": None,
            "google_event_id": None,
            "google_calendar_id": None,
        }
    firestore_meetings.add_document(str(meeting["id"]), updates)
    return updates


# ---------------------------------------------------------------------------------------
# Letting teachers into their own sessions
#
# Meet admitted the organiser and invited guests and made everybody else knock, so these
# functions kept teachers on guest lists and set spaces to "anyone with the link". An LMS room
# admits whoever the LMS lets in, so there is nothing left for them to do; they remain, as
# no-ops, for the admin endpoint and the sweep that still call them.
# ---------------------------------------------------------------------------------------

def invite_teacher_to_meeting(meeting: dict, notify: bool = False) -> dict:
    return {"meeting_id": meeting.get("id"), "status": "SKIPPED",
            "detail": "LMS rooms let their teacher in directly."}


def open_meeting_access(meeting: dict) -> dict:
    return {"meeting_id": meeting.get("id"), "status": "SKIPPED",
            "detail": "LMS rooms admit whoever the LMS lets in."}


def repair_teacher_access(days_ahead: int | None = 2, notify: bool = False) -> dict:
    return {
        "invited": 0, "already": 0, "skipped": 0, "failed": 0, "checked": 0,
        "opened": 0, "open_failed": 0, "failures": [],
    }


async def store_material(
    file: UploadFile,
    class_id: int,
    subject_id: int,
    title: str,
    material_type: str,
    teacher_id: int,
) -> dict:
    """
    Uploads a study material to the configured backend and records it.

    Returns the persisted material document (including its `id`). When the cloud provider
    could not be reached the file is still saved locally and `storage_warning` explains what
    happened - unless STORAGE_STRICT is on, in which case this raises 502 rather than
    pretending the upload reached cloud storage.
    """
    assert_class_and_subject_exist(class_id, subject_id)

    try:
        stored = await storage_service.save_file_detailed(
            file=file, folder=f"class_{class_id}/notes"
        )
    except StorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if stored.get("warning"):
        logger.warning("Material '%s' stored with a warning: %s", title, stored["warning"])

    material_id = firestore_materials.get_next_numeric_id()
    material_data = {
        "class_id": class_id,
        "subject_id": subject_id,
        "teacher_id": teacher_id,
        "title": title,
        "material_type": material_type,
        "file_url": stored["url"],
        "storage_provider": stored["provider"],
        "storage_warning": stored.get("warning"),
        "uploaded_at": datetime.utcnow().isoformat(),
    }
    firestore_materials.add_document(str(material_id), material_data)
    material_data["id"] = material_id
    return material_data


def store_material_link(link_in, teacher_id: int) -> dict:
    """
    Records a web page or YouTube video in the library. Nothing is uploaded: the address is
    validated, a YouTube video id is picked out so it can play in place, and the material
    is filed against the class and subject like any upload.
    """
    assert_class_and_subject_exist(link_in.class_id, link_in.subject_id)
    link = parse_link(link_in.external_url)

    material_type = (link_in.material_type or "").strip().upper() or (
        "VIDEO" if link["link_kind"] == LINK_KIND_YOUTUBE else "LINK"
    )
    material_id = firestore_materials.get_next_numeric_id()
    material_data = {
        "class_id": link_in.class_id,
        "subject_id": link_in.subject_id,
        "teacher_id": teacher_id,
        "title": link_in.title.strip(),
        "material_type": material_type,
        "description": (link_in.description or "").strip() or None,
        # The same address in file_url, so a screen that only knows files still opens it.
        "file_url": link["external_url"],
        "storage_provider": "EXTERNAL",
        **link,
        "uploaded_at": datetime.utcnow().isoformat(),
    }
    firestore_materials.add_document(str(material_id), material_data)
    material_data["id"] = material_id
    return material_data


def material_update_fields(material: dict, update_in) -> dict:
    """
    The fields a metadata edit writes. A new address is only taken on a shared link (an
    uploaded file is replaced by deleting and uploading again) and is re-checked first.
    """
    updated = update_in.model_dump(exclude_unset=True, exclude_none=True)
    new_url = updated.pop("external_url", None)
    if new_url is not None:
        if not material.get("external_url"):
            raise HTTPException(
                status_code=400,
                detail="Only a shared link can change its address. Delete the file and upload again.",
            )
        link = parse_link(new_url)
        updated.update(link)
        updated["file_url"] = link["external_url"]
    if "description" in updated:
        updated["description"] = updated["description"].strip() or None
    return updated
