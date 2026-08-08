from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(slots=True)
class TopicCovered:
    """
    Topic covered entity representing syllabus progress logged by teachers.
    """
    class_id: int
    subject_id: int
    teacher_id: int
    topic_title: str
    description: str | None = None
    date_covered: date = field(default_factory=date.today)
    completion_percentage: float = 100.0
    created_at: datetime = field(default_factory=datetime.utcnow)
    id: int | None = None
