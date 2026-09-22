"""
Response shapes for the unified calendar.

Every source - timetable periods, live classes, tuition sessions, exams, homework - is
flattened to one `CalendarEvent`. `kind` is what a client switches on for colour and icon;
everything else is uniform, so rendering a week never needs to know which collection a row
came from.
"""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CalendarEvent(BaseModel):
    kind: str = Field(
        ...,
        description="TIMETABLE, LIVE_CLASS, TUITION_CLASS, EXAM or HOMEWORK.",
    )
    title: str
    start_at: datetime | None = None
    end_at: datetime | None = None
    # True for items with no time of day - homework due on a date. Render these in the day's
    # header rather than at an hour.
    all_day: bool = False
    due_date: date | None = None

    class_id: int | None = None
    class_name: str | None = None
    subject_id: int | None = None
    subject_name: str | None = None
    teacher_id: int | None = None
    teacher_name: str | None = None
    student_id: int | None = None
    student_name: str | None = None

    status: str | None = None
    # Present only when the viewer may actually join right now. A calendar cannot be used to
    # walk into a room early - the same rule the join endpoint enforces, applied where the
    # link would otherwise leak.
    meeting_link: str | None = None
    reference_id: int | str | None = None
    program: str = "LMS"
    is_cancelled: bool = False
    # The full class clock, on live classes only.
    timing: dict[str, Any] | None = None

    model_config = ConfigDict(from_attributes=True)


class CalendarDay(BaseModel):
    """
    One day in a tile or month view.

    Empty days are included: a month grid needs the blanks, and making a client synthesise
    them from a sparse list is how the last week of a month ends up in the wrong column.
    """
    date: date
    weekday: str
    events: list[CalendarEvent] = Field(default_factory=list)
    count: int = 0


class CalendarOut(BaseModel):
    from_date: date
    to_date: date
    events: list[CalendarEvent] = Field(default_factory=list)
    count: int = 0
    counts_by_kind: dict[str, int] = Field(default_factory=dict)
    # Populated when `group_by_day` is requested - the tile view's shape.
    days: list[CalendarDay] | None = None
