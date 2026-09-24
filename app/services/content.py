"""
Meeting scheduling and study-material storage, shared by the teacher and admin routers.

Both roles perform the same two actions - schedule a live session with a Google Meet link,
and upload a file to the configured storage backend - and the only difference is *whose*
name the record is filed under. Keeping the logic here means an admin acting on behalf of a
teacher gets byte-identical behaviour instead of a second, subtly divergent implementation.
"""

import logging
from datetime import datetime, timedelta

from fastapi import HTTPException, UploadFile

from app.core import google_meet
from app.core.config import settings
from app.core.enums import TEACHING_OR_ADMIN_VALUES
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

# Where a session starts in the recording lifecycle. The rest of it - WAITING, STORED,
# UNAVAILABLE, FAILED - is driven by the sweep in app/services/recordings.py.
RECORDING_NOT_REQUESTED = "NOT_REQUESTED"
RECORDING_ARMED = "ARMED"
RECORDING_ARM_FAILED = "ARM_FAILED"


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
    space_name = None
    meeting_code = None
    owner_email = None
    uses_class_room = False
    auto_record = meeting_in.auto_record
    access_type = None
    access_error = None
    # A hand-entered link belongs to a conference the LMS does not own, so there is nothing
    # to arm and nothing to collect afterwards.
    recording_status = RECORDING_NOT_REQUESTED
    recording_error = None

    # Imported here: class_rooms reads the meetings collection through live_classes, and a
    # top-level import either way would be circular.
    from app.services import class_rooms

    if meeting_link:
        meet_status = MEET_MANUAL
    elif class_rooms.class_room_mode():
        # The classroom model: this period joins the class's one standing room rather than
        # minting a link of its own. Created on first need, so the first period a class
        # ever schedules is what brings its room into being.
        class_room = firestore_classes.get_document(str(meeting_in.class_id)) or {}
        class_room, room_error = class_rooms.ensure_room(
            class_room, actor_id=teacher_id, actor_email=teacher_email
        )
        if class_rooms.has_room(class_room):
            # The scheduling teacher must be able to walk in without asking; make sure they
            # are on the room's guest list (a no-op once they are).
            if teacher_email:
                class_room, _ = class_rooms.sync_room_guests(
                    class_room, extra_emails=[teacher_email], notify=False
                )
            room_fields = class_rooms.meeting_fields_for_room(class_room)
            meeting_link = room_fields["meeting_link"]
            meet_status = room_fields["meet_status"]
            space_name = room_fields["meet_space_name"]
            meeting_code = room_fields["meet_meeting_code"]
            owner_email = room_fields["meet_owner_email"]
            auto_record = room_fields["auto_record"]
            recording_status = room_fields["recording_status"]
            recording_error = room_fields["recording_error"]
            access_type = room_fields["meet_access_type"]
            access_error = room_fields["meet_access_error"]
            uses_class_room = True
        else:
            meet_status = MEET_FAILED
            meet_error = room_error
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
        # A teacher outside the Workspace domain is not the organiser of the event that is
        # about to be created - the school identity is - so without an invitation Meet
        # makes them ask to join their own class. Put them on the guest list.
        if teacher_email and not google_meet.is_own_calendar(teacher_email):
            attendee_emails.append(teacher_email)

        created = google_meet.create_meeting(
            teacher_email=teacher_email,
            title=meeting_in.title,
            scheduled_time=meeting_in.scheduled_time,
            duration_minutes=meeting_in.duration_minutes,
            attendee_emails=attendee_emails,
            auto_record=meeting_in.auto_record,
        )
        meeting_link = created["meeting_link"]
        google_event_id = created["event_id"]
        google_calendar_id = created["calendar_id"]
        meet_error = created["error"]
        meet_status = MEET_CREATED if created["ok"] else MEET_FAILED

        space_name = created["space_name"]
        meeting_code = created["meeting_code"]
        recording_error = created["recording_error"]
        access_type = created.get("access_type")
        access_error = created.get("access_error")
        if not meeting_in.auto_record:
            recording_status = RECORDING_NOT_REQUESTED
        elif created["recording_armed"]:
            recording_status = RECORDING_ARMED
        else:
            # Still swept afterwards: a teacher who records the session by hand should have
            # that video filed too, and the school may authorize the Meet scopes later.
            recording_status = RECORDING_ARM_FAILED

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
        # Recording. `meet_space_name` is the handle the Meet API needs to find this
        # conference's video once the class is over; it cannot be recovered from the Calendar
        # event later, so it is persisted even when arming failed.
        "auto_record": auto_record,
        "meet_space_name": space_name,
        "meet_meeting_code": meeting_code,
        "recording_status": recording_status,
        "recording_error": recording_error,
        # Set when the session uses the class's shared room. `meet_owner_email` is whose
        # Drive its recording lands in - the room's owner, not necessarily this teacher.
        "uses_class_room": uses_class_room,
        "meet_owner_email": owner_email,
        # Who may walk in without asking; None means Google has not (yet) accepted it.
        "meet_access_type": access_type,
        "meet_access_error": access_error,
        "created_at": datetime.utcnow().isoformat(),
    }
    firestore_meetings.add_document(str(meeting_id), meeting_data)
    meeting_data["id"] = meeting_id
    return meeting_data


# ---------------------------------------------------------------------------------------
# Letting teachers into their own sessions
#
# Meet admits the organiser and invited guests without asking and makes everybody else
# knock. A session "created by" a teacher on a gmail address is in fact organised by the
# school's Workspace identity, and until this fix the teacher was not on its guest list -
# so they knocked on their own class. New sessions invite them at creation (above); the
# functions below repair sessions that already exist.
# ---------------------------------------------------------------------------------------

def invite_teacher_to_meeting(meeting: dict, notify: bool = False) -> dict:
    """
    Puts a session's teacher on its Calendar event as a guest, once.

    Returns {"meeting_id", "status", "detail"}; status is INVITED, ALREADY, SKIPPED or
    FAILED. Skipped when the session has no event of its own (a class-room session or a
    pasted link), when the teacher is the organiser, or when it was already done -
    `teacher_invited` is stamped so the sweep never patches the same event twice.
    """
    meeting_id = meeting.get("id")
    if not meeting.get("google_event_id") or meeting.get("uses_class_room"):
        return {"meeting_id": meeting_id, "status": "SKIPPED", "detail": "No Calendar event of its own."}
    if meeting.get("teacher_invited"):
        return {"meeting_id": meeting_id, "status": "ALREADY", "detail": "Already on the guest list."}

    teacher = firestore_users.get_document(str(meeting.get("teacher_id"))) or {}
    teacher_email = (teacher.get("email") or "").strip().lower()
    if not teacher_email:
        return {"meeting_id": meeting_id, "status": "SKIPPED", "detail": "The teacher has no email."}
    if google_meet.is_own_calendar(teacher_email):
        firestore_meetings.add_document(str(meeting_id), {"teacher_invited": True})
        return {"meeting_id": meeting_id, "status": "ALREADY", "detail": "The teacher organises this event."}

    result = google_meet.add_attendees(
        owner_email=teacher_email,
        event_id=meeting["google_event_id"],
        attendee_emails=[teacher_email],
        calendar_id=meeting.get("google_calendar_id"),
        notify=notify,
    )
    if not result["ok"]:
        firestore_meetings.add_document(str(meeting_id), {"teacher_invite_error": result["error"]})
        return {"meeting_id": meeting_id, "status": "FAILED", "detail": result["error"]}

    firestore_meetings.add_document(str(meeting_id), {"teacher_invited": True, "teacher_invite_error": None})
    status = "INVITED" if result["added"] else "ALREADY"
    return {"meeting_id": meeting_id, "status": status, "detail": f"{teacher_email} can join directly."}


def open_meeting_access(meeting: dict) -> dict:
    """
    Sets a session's own Meet space to the configured access type (OPEN), so anyone with
    the link joins without asking. A class-room session inherits its room's setting and is
    skipped here. Returns {"meeting_id", "status", "detail"}: OPENED, ALREADY, SKIPPED or
    FAILED; a failure is stamped with the time so the sweep retries it only every so often.
    """
    from app.core import meet_recordings

    meeting_id = meeting.get("id")
    wanted = (settings.MEET_ACCESS_TYPE or "").strip().upper()
    if not wanted:
        return {"meeting_id": meeting_id, "status": "SKIPPED", "detail": "No access type is configured."}
    if meeting.get("uses_class_room") or not meeting.get("meeting_link"):
        return {"meeting_id": meeting_id, "status": "SKIPPED", "detail": "No Meet space of its own."}
    if (meeting.get("meet_access_type") or "").upper() == wanted:
        return {"meeting_id": meeting_id, "status": "ALREADY", "detail": f"Already {wanted}."}
    if meeting.get("meet_status") == MEET_MANUAL:
        return {"meeting_id": meeting_id, "status": "SKIPPED", "detail": "A pasted link is not a space the LMS can configure."}

    teacher = firestore_users.get_document(str(meeting.get("teacher_id"))) or {}
    result = meet_recordings.set_access_type(
        teacher_email=meeting.get("meet_owner_email") or teacher.get("email") or "",
        meeting_link=meeting["meeting_link"],
    )
    now = datetime.utcnow().isoformat()
    if not result["ok"]:
        firestore_meetings.add_document(
            str(meeting_id), {"meet_access_error": result["error"], "meet_access_checked_at": now}
        )
        return {"meeting_id": meeting_id, "status": "FAILED", "detail": result["error"]}

    firestore_meetings.add_document(str(meeting_id), {
        "meet_access_type": result["access_type"], "meet_access_error": None,
        "meet_access_checked_at": now,
        "meet_space_name": meeting.get("meet_space_name") or result["space_name"],
    })
    return {"meeting_id": meeting_id, "status": "OPENED", "detail": f"Anyone with the link may join ({wanted})."}


# A session Google would not open is retried, but not on every five-minute sweep.
_ACCESS_RETRY = timedelta(minutes=30)


def repair_teacher_access(days_ahead: int | None = 2, notify: bool = False) -> dict:
    """
    Lets teachers into every upcoming session without asking, two ways at once: the
    session's Meet space is set to the configured access type (OPEN - anyone with the link),
    and the teacher is added to the event's guest list for good measure.

    `days_ahead` bounds the pass for the sweep, which runs every few minutes and must not
    re-read the whole history each time; None takes every future session, for the admin's
    one-off repair. Idempotent: guests are stamped once, and a space Google refused to open
    is retried every half hour rather than every pass.
    """
    from app.core import meet_recordings

    today = datetime.utcnow().date()
    limit = (today + timedelta(days=days_ahead)).isoformat() if days_ahead is not None else None
    counts = {
        "invited": 0, "already": 0, "skipped": 0, "failed": 0, "checked": 0,
        "opened": 0, "open_failed": 0, "failures": [],
    }
    now_utc = datetime.utcnow()

    for meeting in firestore_meetings.list_all():
        when = str(meeting.get("scheduled_time") or "")[:10]
        if not when or when < today.isoformat():
            continue
        if limit and when > limit:
            continue
        if meeting.get("uses_class_room") or not meeting.get("meeting_link"):
            continue

        # The space: anyone with the link.
        wanted = (settings.MEET_ACCESS_TYPE or "").strip().upper()
        if wanted and (meeting.get("meet_access_type") or "").upper() != wanted \
                and meeting.get("meet_status") != MEET_MANUAL \
                and meet_recordings.meeting_code_from_link(meeting.get("meeting_link")):
            last = meeting.get("meet_access_checked_at")
            try:
                recent = last and (now_utc - datetime.fromisoformat(str(last))) < _ACCESS_RETRY
            except ValueError:
                recent = False
            if not recent:
                counts["checked"] += 1
                opened = open_meeting_access(meeting)
                if opened["status"] == "OPENED":
                    counts["opened"] += 1
                elif opened["status"] == "FAILED":
                    counts["open_failed"] += 1
                    counts["failures"].append({"meeting_id": meeting.get("id"), "title": meeting.get("title"),
                                               "error": opened["detail"]})

        # The guest list: the teacher by the address the LMS knows.
        if meeting.get("google_event_id") and not meeting.get("teacher_invited"):
            counts["checked"] += 1
            outcome = invite_teacher_to_meeting(meeting, notify=notify)
            key = outcome["status"].lower()
            counts[key] = counts.get(key, 0) + 1
            if outcome["status"] == "FAILED":
                counts["failures"].append({"meeting_id": meeting.get("id"), "title": meeting.get("title"),
                                           "error": outcome["detail"]})

    if counts["invited"] or counts["opened"]:
        logger.info("Teacher access repaired: %d opened, %d invited.", counts["opened"], counts["invited"])
    return counts


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
