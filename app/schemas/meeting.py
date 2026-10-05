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

    # Live-class room. When enabled and no manual meeting_link is supplied, the session gets
    # a room in the LMS (/call/meeting/<id>, or its class's standing room in class-room
    # mode). The name is from the Google Meet era.
    auto_create_meet: bool = True
    duration_minutes: int = 60
    # Kept for older clients. Nobody is invited ahead of time any more: the LMS lets enrolled
    # students into the room when they open it.
    invite_students: bool = True

    # Record the session automatically. Recording starts when the session's teacher joins the
    # LMS call and stops when they leave; the finished video is filed into Blob storage once
    # ACS announces it. Ignored for a hand-entered `meeting_link`, which is not a call the LMS
    # owns.
    auto_record: bool = True


class AdminLiveMeetingCreate(LiveMeetingCreate):
    """
    Admin variant of LiveMeetingCreate.

    `teacher_id` names the teacher the session is filed under - the one who hosts its call
    and whose joining starts its recording. Omit it to schedule the session under the acting
    admin.
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

    # Set on sessions created before the move to Azure, when a Google Calendar event backed
    # the meeting. Always empty on new sessions.
    google_event_id: str | None = None
    google_calendar_id: str | None = None

    # Outcome of giving the session a live-class room, so a missing link is explainable
    # rather than silent (the field names date from Google Meet).
    # MANUAL - a link was supplied by hand; SKIPPED - a room was not requested;
    # CREATED - the session has an LMS room of its own; CLASS_ROOM - it uses its class's
    # standing room; FAILED - a room was asked for and could not be given.
    meet_status: str | None = None
    meet_error: str | None = None

    # Automatic recording. `recording_status` moves NOT_REQUESTED / ARMED -> RECORDING ->
    # WAITING -> STORED, or ends at UNAVAILABLE (nothing was ever recorded) or FAILED (a
    # recording exists but could not be filed). `recording_url` is set once the video is in
    # Blob storage (a /files/ link); `recording_error` explains any status that is not STORED.
    # `recording_drive_file_id` keeps its Drive-era name and now holds the stored file's id.
    auto_record: bool | None = None
    recording_status: str | None = None
    recording_error: str | None = None
    recording_drive_file_id: str | None = None
    recording_stored_at: datetime | None = None
    # Every segment filed for this session, when a class was recorded in more than one.
    recording_files: list[dict] | None = None
    # Whether the teacher has published the recording to the class library. Students see a
    # session's recording only once it is; until then it is the teacher's to review.
    recording_published: bool = False

    duration_minutes: int | None = None
    # True when the session uses its class's one standing room rather than a link of its
    # own (`meet_status` is then CLASS_ROOM). See `app.services.class_rooms`.
    uses_class_room: bool | None = None
    # The Google Meet room's access setting, on sessions from before the move to Azure. An
    # LMS room admits whoever the LMS lets in, so these stay empty on new sessions.
    meet_access_type: str | None = None
    meet_access_error: str | None = None

    model_config = ConfigDict(from_attributes=True)
