from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class LiveMeeting:
    """
    Live meeting and recording link managed by teachers.
    """
    class_id: int
    subject_id: int
    teacher_id: int
    title: str
    meeting_link: str | None = None
    recording_url: str | None = None
    scheduled_time: datetime = field(default_factory=datetime.utcnow)
    status: str = "SCHEDULED"
    created_at: datetime = field(default_factory=datetime.utcnow)
    id: int | None = None
