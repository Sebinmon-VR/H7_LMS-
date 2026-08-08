from datetime import date, datetime
from pydantic import BaseModel, ConfigDict
from app.schemas.academic import ClassRoomOut, SubjectOut
from app.schemas.user import UserOut


class TopicCreate(BaseModel):
    class_id: int
    subject_id: int
    topic_title: str
    description: str | None = None
    date_covered: date | None = None
    completion_percentage: float = 100.0


class TopicOut(BaseModel):
    id: int
    class_id: int
    class_room: ClassRoomOut | None = None
    subject_id: int
    subject: SubjectOut | None = None
    teacher_id: int
    teacher: UserOut | None = None
    topic_title: str
    description: str | None = None
    date_covered: date
    completion_percentage: float
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
