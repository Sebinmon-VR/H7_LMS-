"""
Meeting scheduling and study-material storage, shared by the teacher and admin routers.

Both roles perform the same two actions - schedule a live session with a Google Meet link,
and upload a file to the configured storage backend - and the only difference is *whose*
name the record is filed under. Keeping the logic here means an admin acting on behalf of a
teacher gets byte-identical behaviour instead of a second, subtly divergent implementation.
"""

import logging
from datetime import datetime

from fastapi import HTTPException, UploadFile

from app.core import google_meet
from app.core.enums import UserRole
from app.core.gcp_services import StorageError, storage_service
from app.core.firebase import (
    firestore_classes, firestore_materials, firestore_meetings, firestore_subjects,
    firestore_student_enrollments, firestore_users,
)
from app.schemas.meeting import LiveMeetingCreate

logger = logging.getLogger("content_service")

# Meet generation outcomes recorded on every meeting.
MEET_MANUAL = "MANUAL"     # A link was supplied by hand; nothing was generated.
MEET_SKIPPED = "SKIPPED"   # Generation was not requested.
MEET_CREATED = "CREATED"   # A Meet link was generated successfully.
MEET_FAILED = "FAILED"     # Generation was attempted and did not produce a link.


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

    if teacher.get("role") not in (UserRole.TEACHER.value, UserRole.ADMIN.value):
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
    Creates a live session and, when asked, a real Google Calendar event with a Meet link.

    Returns the persisted meeting document (including its `id`). The meeting is always
    saved: if Meet generation fails, `meet_status` is FAILED and `meet_error` carries the
    reason, so the schedule survives and the cause is visible in the API response instead of
    only in the server log.
    """
    assert_class_and_subject_exist(meeting_in.class_id, meeting_in.subject_id)

    meeting_link = meeting_in.meeting_link
    google_event_id = None
    google_calendar_id = None
    meet_error = None

    if meeting_link:
        meet_status = MEET_MANUAL
    elif not meeting_in.auto_create_meet:
        meet_status = MEET_SKIPPED
    else:
        attendee_emails = []
        if meeting_in.invite_students:
            attendee_emails = [
                student["email"]
                for student in active_students_in_class(meeting_in.class_id)
                if student.get("email")
            ]

        created = google_meet.create_meeting(
            teacher_email=teacher_email,
            title=meeting_in.title,
            scheduled_time=meeting_in.scheduled_time,
            duration_minutes=meeting_in.duration_minutes,
            attendee_emails=attendee_emails,
        )
        meeting_link = created["meeting_link"]
        google_event_id = created["event_id"]
        google_calendar_id = created["calendar_id"]
        meet_error = created["error"]
        meet_status = MEET_CREATED if created["ok"] else MEET_FAILED

    meeting_id = firestore_meetings.get_next_numeric_id()
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
        "google_event_id": google_event_id,
        "google_calendar_id": google_calendar_id,
        "meet_status": meet_status,
        "meet_error": meet_error,
        "created_at": datetime.utcnow().isoformat(),
    }
    firestore_meetings.add_document(str(meeting_id), meeting_data)
    meeting_data["id"] = meeting_id
    return meeting_data


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
    pretending the upload reached Drive or Cloud Storage.
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
