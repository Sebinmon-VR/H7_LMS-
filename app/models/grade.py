from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class ExamGrade:
    """
    Evaluation or exam grade entry per student, per subject.
    """
    student_id: int
    class_id: int
    subject_id: int
    teacher_id: int
    exam_name: str
    marks_obtained: float
    max_marks: float = 100.0
    remarks: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    id: int | None = None
