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
    """
    A mark on a student's record.

    Rows arrive here two ways: entered by hand through `/teachers/grades`, or mirrored from a
    published exam by the exam module. The exam module is why `marks_obtained` is nullable and
    `grade` exists - an exam valued on a letter scale has a grade and no marks, and inventing
    a number for it would put fabricated data on a report card.
    """
    id: int
    student_id: int
    student: UserOut | None = None
    # Null on a tuition record, which belongs to one student rather than a class.
    class_id: int | None = None
    class_room: ClassRoomOut | None = None
    subject_id: int
    subject: SubjectOut | None = None
    teacher_id: int
    teacher: UserOut | None = None
    exam_name: str
    marks_obtained: float | None = None
    max_marks: float | None = None
    # Set when this row came from a letter-graded exam, or from one whose scale defines bands.
    grade: str | None = None
    remarks: str | None = None
    created_at: datetime
    # The exam this mark came from, when it was mirrored from the exam module rather than
    # typed in by hand. Lets a client link a mark back to the script it was awarded for.
    exam_id: int | None = None

    model_config = ConfigDict(from_attributes=True)
