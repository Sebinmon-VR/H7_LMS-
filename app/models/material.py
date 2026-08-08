from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class StudyMaterial:
    """
    Study material entity for uploading notes, books, and resources.
    """
    class_id: int
    subject_id: int
    teacher_id: int
    title: str
    material_type: str = "NOTES"
    file_url: str = ""
    uploaded_at: datetime = field(default_factory=datetime.utcnow)
    id: int | None = None
