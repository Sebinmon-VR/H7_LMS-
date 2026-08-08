import enum
from dataclasses import dataclass, field
from datetime import date, datetime


class AttendanceStatus(str, enum.Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LATE = "LATE"
    EXCUSED = "EXCUSED"


@dataclass(slots=True)
class AttendanceRecord:
    """
    Attendance record stored per student, per class, per subject, and date.
    """
    student_id: int
    class_id: int
    subject_id: int
    teacher_id: int
    date: date
    status: AttendanceStatus = AttendanceStatus.PRESENT
    remarks: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    id: int | None = None
