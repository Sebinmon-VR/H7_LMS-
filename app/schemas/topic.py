from datetime import date, datetime
from pydantic import BaseModel, ConfigDict, Field
from app.schemas.academic import ClassRoomOut, SubjectOut
from app.schemas.user import UserOut


class TopicAttachmentOut(BaseModel):
    """
    A file the teacher attached to a logged topic: notes (any document), an image or a
    voice clip recorded in the browser. Stored where study materials are; students see it
    with the topic in their syllabus.
    """
    id: str
    # NOTE, IMAGE or AUDIO.
    kind: str
    file_name: str | None = None
    file_url: str
    content_type: str | None = None
    size_bytes: int | None = None
    storage_provider: str | None = None
    storage_warning: str | None = None
    uploaded_at: datetime | None = None
    caption: str | None = None


class PendingTopicOut(BaseModel):
    """
    A period of the teacher's that has ended today with no topic logged against it yet -
    what the dashboard prompts them to fill in as soon as the class is over.
    """
    class_id: int
    class_name: str
    subject_id: int
    subject_name: str
    on_date: date
    starts_at: datetime
    ends_at: datetime
    period_label: str | None = None
    minutes_since_end: int


class TopicCreate(BaseModel):
    class_id: int
    subject_id: int
    topic_title: str
    description: str | None = None
    date_covered: date | None = None
    completion_percentage: float = 100.0


class TopicUpdate(BaseModel):
    """Partial update for a logged topic; omitted fields are left unchanged."""
    topic_title: str | None = None
    description: str | None = None
    date_covered: date | None = None
    completion_percentage: float | None = None


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
    attachments: list[TopicAttachmentOut] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)
