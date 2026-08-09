from datetime import datetime
from pydantic import BaseModel, ConfigDict
from app.schemas.academic import SubjectOut, ClassRoomOut
from app.schemas.user import UserOut


class GradeEntryCreate(BaseModel):
    student_id: int
    class_id: int
    subject_id: int
    exam_name: str
    marks_obtained: float
    max_marks: float = 100.0
    remarks: str | None = None


class GradeEntryUpdate(BaseModel):
    """Partial update for an exam grade; omitted fields are left unchanged."""
    exam_name: str | None = None
    marks_obtained: float | None = None
    max_marks: float | None = None
    remarks: str | None = None


class ExamGradeOut(BaseModel):
    id: int
    student_id: int
    student: UserOut | None = None
    class_id: int
    class_room: ClassRoomOut | None = None
    subject_id: int
    subject: SubjectOut | None = None
    teacher_id: int
    teacher: UserOut | None = None
    exam_name: str
    marks_obtained: float
    max_marks: float
    remarks: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
