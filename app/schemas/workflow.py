"""
Request and response shapes for extra classes, staff leave, support tickets and homework.
"""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.enums import (
    ExtraClassStatus, HomeworkStatus, LeaveDayPart, LeaveStatus, LeaveType,
    Program, TicketCategory, TicketPriority, TicketStatus,
)


# ---------------------------------------------------------------------------------------
# Extra classes
# ---------------------------------------------------------------------------------------

class ExtraClassCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    scheduled_time: datetime
    duration_minutes: int = Field(45, ge=5, le=480)
    reason: str | None = Field(
        None, max_length=2000,
        description="Why the class is needed. Read by whoever approves it.",
    )

    program: Program = Program.LMS
    # School: both required. Tuition: neither; give `enrollment_id` instead.
    class_id: int | None = None
    subject_id: int | None = None
    enrollment_id: int | str | None = None

    @model_validator(mode="after")
    def _names_one_target(self):
        """
        A request must name exactly one thing it is a class for.

        Carrying both a class and an enrollment has no single answer to "who is this class
        for?", and the failure would not surface until somebody tried to schedule it.
        """
        if self.program == Program.TUITION:
            if not self.enrollment_id:
                raise ValueError("A tuition extra class needs 'enrollment_id'.")
            if self.class_id or self.subject_id:
                raise ValueError(
                    "A tuition extra class hangs off an enrollment, not a class and subject."
                )
        else:
            if not self.class_id or not self.subject_id:
                raise ValueError(
                    "A school extra class needs both 'class_id' and 'subject_id'."
                )
            if self.enrollment_id:
                raise ValueError(
                    "A school extra class hangs off a class and subject, not an enrollment."
                )
        return self


class ExtraClassDecision(BaseModel):
    approve: bool
    note: str | None = Field(
        None, max_length=2000,
        description="Required when rejecting - the teacher needs a reason they can read.",
    )


class ExtraClassOut(BaseModel):
    id: int
    requested_by: int
    teacher_name: str | None = None
    title: str
    scheduled_time: datetime
    duration_minutes: int = 45
    program: str
    reason: str | None = None

    class_id: int | None = None
    class_name: str | None = None
    subject_id: int | None = None
    subject_name: str | None = None
    enrollment_id: int | str | None = None

    status: ExtraClassStatus
    decided_by: int | None = None
    decided_by_name: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None

    # Set once the approved class actually exists. An APPROVED request with neither is an
    # approval whose class has not been created yet - a visible, retryable state.
    created_meeting_id: int | None = None
    created_session_id: str | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------------------
# Live class timing
# ---------------------------------------------------------------------------------------

class ClassTimingOut(BaseModel):
    """
    The class clock, computed server-side.

    Bind a join button to `may_join` and nothing else: it already accounts for the early
    window, the grace period, the status and whether the class has begun. `join_blocked_reason`
    is the message to show when it is false.
    """
    now: datetime
    scheduled_start_at: datetime | None = None
    scheduled_end_at: datetime | None = None
    duration_minutes: int = 45
    starts_in_minutes: float | None = None
    minutes_remaining: float | None = None

    class_started_at: datetime | None = None
    class_has_started: bool = False
    is_live: bool = False
    is_closed: bool = False
    is_expired: bool = False

    auto_start: bool = False
    join_opens_at: datetime | None = None
    join_window_open: bool = False
    may_join: bool = False
    waiting_for_teacher: bool = False
    join_blocked_reason: str | None = None


class JoinClassOut(BaseModel):
    """What a client needs to actually enter a class. The link is absent unless it may join."""
    meeting_id: int
    title: str
    meeting_link: str | None = None
    timing: ClassTimingOut


class ClassRoomAccessOut(BaseModel):
    """
    A class's standing room, as one person sees it right now.

    The school runs one Google Meet room per class: students join it and stay, and each
    subject teacher joins at their period. `may_join` is the single flag a join button
    binds to - for a student it is true while any period of the class is on (within the
    join window) or any scheduled session is live; a teacher or admin may always enter.
    `join_blocked_reason` is the sentence to show when it is false.
    """
    class_id: int
    class_name: str
    class_code: str | None = None

    has_room: bool = False
    # GOOGLE_MEET or MANUAL.
    room_provider: str | None = None
    # NONE, CREATED, MANUAL or FAILED; `room_error` explains FAILED.
    room_status: str | None = None
    room_error: str | None = None
    room_recording_status: str | None = None
    # Whether the school shares rooms per class at all (the admin setting).
    class_room_mode: bool = True
    is_host: bool = False

    now: datetime
    may_join: bool = False
    join_blocked_reason: str | None = None

    current_period: Any | None = None
    next_period: Any | None = None
    periods_today: list[Any] = Field(default_factory=list)
    # The day's window: a student joins once at `day_opens_at` and stays until
    # `day_closes_at`, breaks included.
    day_opens_at: datetime | None = None
    day_closes_at: datetime | None = None
    # This person's own window: a teacher's runs around their period (current or next), a
    # student's is the day, an admin has none. `my_*_period` are a teacher's own periods.
    window_opens_at: datetime | None = None
    window_closes_at: datetime | None = None
    my_current_period: Any | None = None
    my_next_period: Any | None = None
    # The caller is this class's class teacher: they get the students' whole-day window.
    leads_class: bool = False
    # Admins may always enter; every other host is bound to their own periods.
    is_admin: bool = False
    live_meeting_ids: list[int] = Field(default_factory=list)

    # The caller's own last movement today (JOINED_ROOM / LEFT_ROOM ...), for "you joined
    # at 9:02" and the leave button. `in_room` is true when that last word was a join.
    my_last_action: str | None = None
    my_last_at: datetime | None = None
    in_room: bool = False

    # Present only when the caller may enter, the same rule the session join applies.
    room_link: str | None = None


class JoinRoomOut(BaseModel):
    class_id: int
    class_name: str
    room_link: str | None = None
    access: ClassRoomAccessOut


class ClassRoomEventOut(BaseModel):
    """
    One line of a class's room log: a join the LMS handed a link out for, a period a teacher
    opened or closed, a room made or removed. What the LMS itself saw - Google does not
    report who is inside a Meet to this app.
    """
    id: int | None = None
    class_id: int
    meeting_id: int | None = None
    user_id: int | None = None
    user_name: str | None = None
    role: str | None = None
    # JOINED_ROOM, JOINED_SESSION, STARTED, ENDED, ROOM_CREATED, ROOM_REPLACED,
    # ROOM_LINK_SET, ROOM_CLEARED.
    action: str
    detail: str | None = None
    at: datetime


class LiveSessionOut(BaseModel):
    meeting_id: int
    title: str | None = None
    subject_name: str | None = None
    teacher_name: str | None = None
    timing: ClassTimingOut


class LiveClassBoardRow(ClassRoomAccessOut):
    """
    One class on the admin's live board: the room and today's clock, plus who has come in.

    `students_joined_today` counts distinct students the LMS let into the room or a session
    today; `teacher_present` says a teacher came in since the current period opened.
    Neither can see somebody who left, so they are attendance as the LMS knows it, not as
    Google does.
    """
    enrolled_count: int = 0
    students_joined_today: int = 0
    teachers_joined_today: list[str] = Field(default_factory=list)
    teacher_present: bool = False
    # In the room right now by the LMS's reckoning: their last word today was a join.
    students_in_now: list[str] = Field(default_factory=list)
    teachers_in_now: list[str] = Field(default_factory=list)
    live_sessions: list[LiveSessionOut] = Field(default_factory=list)
    last_event: ClassRoomEventOut | None = None
    is_live: bool = False
    # Meet's own attendance record: when it was last copied, or why it cannot be yet.
    room_attendance_synced_at: datetime | None = None
    room_attendance_error: str | None = None


class RoomPresenceRow(BaseModel):
    user_id: int
    name: str
    in_room: bool = False
    last_action: str | None = None
    last_at: datetime | None = None


class RoomPresenceOut(BaseModel):
    """
    Who is in a class's room right now, by name, from the LMS log: a person is in when
    their last word today was a join, out when it was a leave. For the teacher in front of
    the class and for the office; Meet's own record is `ClassRoomAttendanceOut`.
    """
    class_id: int
    class_name: str
    now: datetime
    enrolled_count: int = 0
    in_count: int = 0
    students: list[RoomPresenceRow] = Field(default_factory=list)
    teachers_in: list[RoomPresenceRow] = Field(default_factory=list)
    meet_attendance_synced_at: datetime | None = None


class ClassRoomAttendanceOut(BaseModel):
    """
    One participant of one Meet conference in a class's room, as Google recorded it: when
    they joined and left, in sessions. Meet gives a display name, not an email, so
    `matched_user_*` is the LMS's best match by name and may be empty for a guest.
    """
    id: str | None = None
    class_id: int
    date: date
    conference_record: str | None = None
    conference_start_at: datetime | None = None
    conference_end_at: datetime | None = None
    participant: str | None = None
    display_name: str | None = None
    # SIGNED_IN, ANONYMOUS or PHONE.
    user_kind: str | None = None
    matched_user_id: int | None = None
    matched_user_name: str | None = None
    matched_role: str | None = None
    sessions: list[dict] = Field(default_factory=list)
    first_joined_at: datetime | None = None
    last_left_at: datetime | None = None
    still_in: bool = False
    minutes: float = 0
    synced_at: datetime | None = None


# ---------------------------------------------------------------------------------------
# Staff leave
# ---------------------------------------------------------------------------------------

class LeaveApply(BaseModel):
    leave_type: LeaveType
    start_date: date
    end_date: date
    day_part: LeaveDayPart = LeaveDayPart.FULL_DAY
    reason: str | None = Field(None, max_length=2000)
    contact_during_leave: str | None = Field(None, max_length=120)
    attachments: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _dates_make_sense(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date must fall on or after start_date.")
        if self.day_part != LeaveDayPart.FULL_DAY and self.end_date != self.start_date:
            raise ValueError(
                "A half-day request covers one date; set start_date and end_date the same."
            )
        return self


class LeaveDecision(BaseModel):
    approve: bool
    note: str | None = Field(
        None, max_length=2000, description="Required when rejecting."
    )
    substitute_teacher_id: int | None = Field(
        None, description="Optional: who covers the affected periods."
    )


class AffectedPeriod(BaseModel):
    date: date
    class_id: int | None = None
    class_name: str | None = None
    subject_name: str | None = None
    start_time: str | None = None
    end_time: str | None = None


class LeaveRequestOut(BaseModel):
    id: int
    teacher_id: int
    teacher_name: str | None = None
    employee_id: str | None = None
    leave_type: LeaveType
    start_date: date
    end_date: date
    day_part: LeaveDayPart
    total_days: float
    reason: str | None = None
    contact_during_leave: str | None = None

    status: LeaveStatus
    decided_by: int | None = None
    decided_by_name: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    substitute_teacher_id: int | None = None
    substitute_teacher_name: str | None = None

    # What the applicant was timetabled to teach, snapshotted when they applied. Kept so the
    # approver sees what they are agreeing to cover, and so it stays answerable later.
    affected_periods: list[AffectedPeriod] = Field(default_factory=list)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class LeaveBalanceOut(BaseModel):
    """
    One teacher's leave, totalled by type.

    `pending_days` is separate from `taken_days` because a request awaiting a decision is
    neither granted nor free - showing them as one number is how two people get approved for
    the same week.
    """
    teacher_id: int
    teacher_name: str | None = None
    academic_year_id: int | None = None
    taken_days: dict[str, float] = Field(default_factory=dict)
    pending_days: dict[str, float] = Field(default_factory=dict)
    total_taken: float = 0.0
    total_pending: float = 0.0
    request_count: int = 0


# ---------------------------------------------------------------------------------------
# Support tickets
# ---------------------------------------------------------------------------------------

class TicketCreate(BaseModel):
    subject: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1, max_length=20000)
    category: TicketCategory = TicketCategory.OTHER
    priority: TicketPriority = TicketPriority.NORMAL
    program: Program = Program.LMS
    about_student_id: int | None = Field(
        None, description="Set when the ticket concerns a specific student."
    )
    attachments: list[dict[str, Any]] = Field(default_factory=list)


class TicketReply(BaseModel):
    body: str = Field(..., min_length=1, max_length=20000)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    is_internal: bool = Field(
        False,
        description="Staff-only note the reporter never sees. Ignored for non-staff.",
    )


class TicketUpdate(BaseModel):
    """[Staff] Partial update; omitted fields are left unchanged."""
    status: TicketStatus | None = None
    priority: TicketPriority | None = None
    category: TicketCategory | None = None
    assigned_to: int | None = None
    resolution_note: str | None = Field(None, max_length=2000)


class TicketMessageOut(BaseModel):
    author_id: int | None = None
    author_name: str | None = None
    author_role: str | None = None
    body: str
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    is_internal: bool = False
    sent_at: datetime | None = None


class TicketOut(BaseModel):
    id: int
    subject: str
    raised_by: int
    raised_by_name: str | None = None
    raised_by_role: str | None = None
    program: str
    category: TicketCategory
    priority: TicketPriority
    status: TicketStatus

    messages: list[TicketMessageOut] = Field(default_factory=list)
    message_count: int = 0

    assigned_to: int | None = None
    assigned_to_name: str | None = None
    about_student_id: int | None = None
    about_student_name: str | None = None

    first_response_at: datetime | None = None
    resolved_at: datetime | None = None
    closed_at: datetime | None = None
    resolution_note: str | None = None
    satisfaction_rating: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class TicketRating(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    comment: str | None = Field(None, max_length=1000)


class SupportContactOut(BaseModel):
    """
    The 'call' half of support: who to ring, and when.

    Per product, because a tuition parent and a school parent are usually given different
    numbers, and one shared number that reaches the wrong desk is worse than two.
    """
    program: str
    phone: str | None = None
    alternate_phone: str | None = None
    whatsapp: str | None = None
    email: str | None = None
    hours: str | None = None
    address: str | None = None
    notes: str | None = None
    tickets_enabled: bool = True


class SupportContactUpdate(BaseModel):
    phone: str | None = Field(None, max_length=32)
    alternate_phone: str | None = Field(None, max_length=32)
    whatsapp: str | None = Field(None, max_length=32)
    email: str | None = Field(None, max_length=320)
    hours: str | None = Field(None, max_length=200, description='e.g. "Mon-Fri, 9am-5pm"')
    address: str | None = Field(None, max_length=500)
    notes: str | None = Field(None, max_length=2000)
    tickets_enabled: bool | None = None


# ---------------------------------------------------------------------------------------
# Homework
# ---------------------------------------------------------------------------------------

class HomeworkCreate(BaseModel):
    class_id: int
    subject_id: int
    title: str = Field(..., min_length=1, max_length=200)
    due_date: date
    description: str | None = Field(None, max_length=10000)
    assigned_date: date | None = Field(None, description="Defaults to today.")
    max_marks: float | None = Field(None, ge=0)
    is_mandatory: bool = Field(
        True, description="Counts towards the daily-homework obligation."
    )
    allow_late_submission: bool = True
    attachments: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _due_after_assigned(self):
        if self.assigned_date and self.due_date < self.assigned_date:
            raise ValueError("due_date must fall on or after assigned_date.")
        return self


class HomeworkUpdate(BaseModel):
    """Partial update; omitted fields are left unchanged."""
    title: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=10000)
    due_date: date | None = None
    max_marks: float | None = Field(None, ge=0)
    is_mandatory: bool | None = None
    allow_late_submission: bool | None = None
    attachments: list[dict[str, Any]] | None = None


class HomeworkOut(BaseModel):
    id: int
    class_id: int
    class_name: str | None = None
    subject_id: int
    subject_name: str | None = None
    teacher_id: int
    teacher_name: str | None = None
    title: str
    description: str | None = None
    assigned_date: date
    due_date: date
    max_marks: float | None = None
    is_mandatory: bool = True
    allow_late_submission: bool = True
    attachments: list[dict[str, Any]] = Field(default_factory=list)

    # Derived at read time; never stored.
    is_overdue: bool = False
    days_until_due: int | None = None
    # Present on a teacher's view.
    submission_count: int | None = None
    expected_count: int | None = None
    # Present on a student's view.
    my_status: HomeworkStatus | None = None
    my_marks: float | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class HomeworkSubmit(BaseModel):
    body: str | None = Field(None, max_length=20000)
    attachments: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _has_something(self):
        if not (self.body or "").strip() and not self.attachments:
            raise ValueError("A submission needs either written work or an attachment.")
        return self


class HomeworkGrade(BaseModel):
    marks: float | None = Field(None, ge=0)
    feedback: str | None = Field(None, max_length=5000)


class HomeworkSubmissionOut(BaseModel):
    id: str
    assignment_id: int
    assignment_title: str | None = None
    student_id: int
    student_name: str | None = None
    status: HomeworkStatus
    body: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    submitted_at: datetime | None = None
    is_late: bool = False
    marks: float | None = None
    max_marks: float | None = None
    feedback: str | None = None
    graded_by: int | None = None
    graded_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)
