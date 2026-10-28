import datetime as dt
from datetime import date, datetime
from typing import List
from pydantic import BaseModel, ConfigDict
from app.core.enums import AttendanceStatus
from app.schemas.user import UserOut
from app.schemas.academic import ClassRoomOut, SubjectOut


class StudentAttendanceItem(BaseModel):
    student_id: int
    status: AttendanceStatus = AttendanceStatus.PRESENT
    remarks: str | None = None


class BatchAttendanceCreate(BaseModel):
    class_id: int
    subject_id: int
    date: date
    attendance_list: List[StudentAttendanceItem]


class AttendanceUpdate(BaseModel):
    """Partial update for a single attendance record; omitted fields are left unchanged."""
    status: AttendanceStatus | None = None
    remarks: str | None = None
    # Qualified via the module alias: the field name `date` shadows the imported type.
    date: dt.date | None = None


class AttendanceOut(BaseModel):
    id: int
    student_id: int
    student: UserOut | None = None
    class_id: int
    class_room: ClassRoomOut | None = None
    subject_id: int
    subject: SubjectOut | None = None
    teacher_id: int
    date: date
    status: AttendanceStatus
    remarks: str | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PeriodRegisterRow(BaseModel):
    """One student on a period's register, with what the class room's log says."""
    student_id: int
    name: str
    # What the join log suggests: PRESENT, LATE (joined more than late_after_minutes in) or ABSENT.
    suggested: AttendanceStatus
    # The saved mark if the register was taken, otherwise the suggestion.
    status: AttendanceStatus
    saved_status: AttendanceStatus | None = None
    remarks: str | None = None
    joined_at: datetime | None = None
    minutes: float = 0.0
    in_room_now: bool = False


class PeriodRegisterOut(BaseModel):
    """A teacher's period register in the live class: suggested from who joined, editable."""
    class_id: int
    class_name: str
    subject_id: int
    subject_name: str | None = None
    date: date
    starts_at: datetime
    ends_at: datetime
    taken: bool = False
    taken_by: str | None = None
    late_after_minutes: int = 10
    students: List[PeriodRegisterRow] = []


class PeriodRegisterSave(BaseModel):
    """The marks a teacher confirmed for their period."""
    subject_id: int
    date: date
    attendance_list: List[StudentAttendanceItem]
