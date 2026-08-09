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
