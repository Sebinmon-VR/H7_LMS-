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

    model_config = ConfigDict(from_attributes=True)
