"""
Weekly class timetable.

An entry is one recurring period: "Class 7A does Maths with Mr Rahman on Tuesdays,
09:00-09:45, in Room 12". Entries recur every week until `effective_to` passes, so a term's
schedule is a few dozen rows rather than one row per calendar day.

Times are stored as naive local `time` values interpreted in SCHOOL_TIMEZONE. A period is a
wall-clock fact - assembly is at 08:00 whether or not the clocks changed last weekend - so
storing an absolute instant would be wrong here in a way it is not for a meeting.
"""

from datetime import date, datetime, time
from typing import List

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.enums import DayOfWeek
from app.schemas.academic import ClassRoomOut, SubjectOut
from app.schemas.user import UserOut


class TimetableEntryBase(BaseModel):
    class_id: int
    subject_id: int
    teacher_id: int | None = Field(
        None,
        description="Teacher taking the period. Defaults to the teacher mapped to this "
                    "subject and class when omitted.",
    )
    day_of_week: DayOfWeek
    start_time: time = Field(..., description="Local start time, e.g. 09:00")
    end_time: time = Field(..., description="Local end time, e.g. 09:45")
    room: str | None = Field(None, max_length=50, description="Room or lab name")
    period_label: str | None = Field(
        None, max_length=50, description="Display label, e.g. 'Period 1'"
    )
    effective_from: date | None = Field(
        None, description="First date this period applies. Defaults to today."
    )
    effective_to: date | None = Field(
        None, description="Last date this period applies. Open-ended when omitted."
    )
    is_active: bool = True

    @model_validator(mode="after")
    def _check_ordering(self):
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be later than start_time")
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("effective_to cannot be earlier than effective_from")
        return self


class TimetableEntryCreate(TimetableEntryBase):
    """One period. `POST /admin/timetable`."""


class TimetableBulkCreate(BaseModel):
    """
    A whole week (or term) in one request.

    Building a timetable one period at a time means ~40 round trips and 40 chances to leave
    a class half-scheduled. `replace_existing` makes the operation idempotent: re-posting a
    corrected week overwrites the old one rather than doubling it up.
    """
    entries: List[TimetableEntryCreate] = Field(..., min_length=1, max_length=500)
    replace_existing: bool = Field(
        False,
        description="Delete the current timetable for every class named in `entries` before "
                    "inserting. Use when re-uploading a corrected schedule.",
    )


class TimetableEntryUpdate(BaseModel):
    """Partial update; omitted fields are left unchanged."""
    class_id: int | None = None
    subject_id: int | None = None
    teacher_id: int | None = None
    day_of_week: DayOfWeek | None = None
    start_time: time | None = None
    end_time: time | None = None
    room: str | None = Field(None, max_length=50)
    period_label: str | None = Field(None, max_length=50)
    effective_from: date | None = None
    effective_to: date | None = None
    is_active: bool | None = None


class TimetableEntryOut(BaseModel):
    id: int
    class_id: int
    class_room: ClassRoomOut | None = None
    subject_id: int
    subject: SubjectOut | None = None
    teacher_id: int | None = None
    teacher: UserOut | None = None
    day_of_week: DayOfWeek
    start_time: time
    end_time: time
    room: str | None = None
    period_label: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    is_active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class TimetableBulkResult(BaseModel):
    """Outcome of a bulk upload, including the rows that were rejected and why."""
    created: List[TimetableEntryOut] = Field(default_factory=list)
    replaced: int = Field(0, description="Existing entries deleted because replace_existing was set")
    skipped: List[dict] = Field(
        default_factory=list,
        description="Entries that could not be created: {index, reason}",
    )
    detail: str


class ScheduledPeriod(BaseModel):
    """
    A timetable entry resolved against a specific calendar date.

    This is what a "today's classes" or "what's next" view renders: the recurring entry
    plus the concrete start and end instants it maps to, so the client does not have to
    redo weekday and timezone arithmetic.
    """
    entry: TimetableEntryOut
    on_date: date
    starts_at: datetime
    ends_at: datetime
    starts_in_minutes: int | None = Field(
        None, description="Minutes until start. Negative once the period has begun."
    )
    is_current: bool = False
