from datetime import datetime
from pydantic import BaseModel, ConfigDict
from app.schemas.academic import ClassRoomOut, SubjectOut
from app.schemas.user import UserOut


class LiveMeetingCreate(BaseModel):
    class_id: int
    subject_id: int
    title: str
    meeting_link: str | None = None
    recording_url: str | None = None
    scheduled_time: datetime
    status: str = "SCHEDULED"

    # Google Meet generation. When enabled and no manual meeting_link is supplied, the
    # backend creates a real Calendar event owned by the teacher and invites the class.
    auto_create_meet: bool = True
    duration_minutes: int = 60
    invite_students: bool = True


class AdminLiveMeetingCreate(LiveMeetingCreate):
    """
    Admin variant of LiveMeetingCreate.

    `teacher_id` names the teacher the session is filed under, and whose calendar the Google
    Meet event is created on. Omit it to schedule the session under the acting admin.
    """
    teacher_id: int | None = None


class LiveMeetingUpdate(BaseModel):
    """Partial update for a scheduled meeting; omitted fields are left unchanged."""
    title: str | None = None
    meeting_link: str | None = None
    recording_url: str | None = None
    scheduled_time: datetime | None = None
    status: str | None = None
    duration_minutes: int | None = None


class LiveMeetingOut(BaseModel):
    id: int
    class_id: int
    class_room: ClassRoomOut | None = None
    subject_id: int
    subject: SubjectOut | None = None
    teacher_id: int
    teacher: UserOut | None = None
    title: str
    meeting_link: str | None = None
    recording_url: str | None = None
    scheduled_time: datetime
    status: str
    created_at: datetime

    # Populated when the meeting is backed by a Google Calendar event.
    google_event_id: str | None = None
    google_calendar_id: str | None = None

    # Outcome of Meet link generation, so a missing link is explainable rather than silent.
    # MANUAL - a link was supplied by hand; SKIPPED - generation was not requested;
    # CREATED - a Meet link was generated; FAILED - generation was attempted and did not work.
    meet_status: str | None = None
    meet_error: str | None = None

    model_config = ConfigDict(from_attributes=True)
