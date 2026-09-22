"""
Requests that need somebody's decision: extra classes, staff leave, and support tickets.

Three subjects, one shape - asked for, decided on, and kept afterwards. They share this
module because the discipline they need is identical and easy to get wrong in the same way:
the decision must be recorded with who made it and when, the request must survive its own
rejection, and the thing the approval produces (a class, an absence, a reply) must be a
separate step from the approval itself.

That last point is the one worth stating. An approval that also creates the class in the same
write cannot report "approved, but the Meet link failed" - it either lies or rolls back a
decision a human already made. Keeping them apart means a request can sit in APPROVED with no
class yet, which is visible and fixable, instead of silently reverting to pending.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.core.enums import (
    ExtraClassStatus, HomeworkStatus, LeaveDayPart, LeaveStatus, LeaveType,
    TicketCategory, TicketPriority, TicketStatus,
)


@dataclass(slots=True)
class ExtraClassRequest:
    """
    A teacher asking to hold a class outside the timetable.

    The brief asks that extra classes need admin approval, and this is where that is
    enforced. It covers both products: a school request names a class and a subject, a
    tuition request names an enrollment, and exactly one of those is set.

    `created_meeting_id` and `created_session_id` are filled in when the approved class is
    actually made. Until one of them is set, an APPROVED request is an approval that has not
    yet produced a class - a state that has to be visible, because the creation can fail on a
    timetable clash or a Meet error long after the admin has pressed approve.
    """
    requested_by: int
    title: str
    scheduled_time: datetime

    program: str = "LMS"
    duration_minutes: int = 45
    reason: str | None = None

    # School: the class and subject being taught.
    class_id: int | None = None
    subject_id: int | None = None
    # Tuition: the one arrangement the extra class belongs to.
    enrollment_id: int | None = None

    status: ExtraClassStatus = ExtraClassStatus.PENDING
    decided_by: int | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None

    created_meeting_id: int | None = None
    created_session_id: str | None = None

    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class LeaveRequest:
    """
    A member of staff applying to be away.

    `total_days` is stored rather than recomputed on read because the leave *balance* is a
    running total over these records, and a balance that changes when somebody edits the
    half-day rules years later is not a balance. It is computed once, at application time,
    from the dates and the day parts.

    `affected_periods` is a snapshot of what the applicant was timetabled to teach across the
    dates, taken when the request is made. Kept on the record so the approver can see what
    they are actually agreeing to cover, and so it stays answerable after the timetable has
    moved on.
    """
    teacher_id: int
    leave_type: LeaveType
    start_date: date
    end_date: date

    day_part: LeaveDayPart = LeaveDayPart.FULL_DAY
    total_days: float = 1.0
    reason: str | None = None
    contact_during_leave: str | None = None

    status: LeaveStatus = LeaveStatus.PENDING
    decided_by: int | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None

    # Who is covering. Optional: many schools decide this separately, and forcing it at
    # approval time would block approvals on a decision nobody has made yet.
    substitute_teacher_id: int | None = None

    # [{"timetable_entry_id", "date", "class_id", "class_name", "subject_name",
    #   "start_time", "end_time"}]
    affected_periods: list[dict[str, Any]] = field(default_factory=list)

    # [{"file_name", "file_url"}] - a medical certificate, usually.
    attachments: list[dict[str, Any]] = field(default_factory=list)

    program: str = "LMS"
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class SupportTicket:
    """
    A question or a problem raised by somebody using the system.

    The brief asks for support by chat and by phone, per product. This is the chat half, as a
    ticket with a thread rather than live messaging: a ticket survives the browser closing,
    can be picked up by whoever is on duty, and leaves a record of what was promised. The
    phone half is contact details in the admin settings, which need no records at all.

    `messages` is held inline rather than as its own collection. A ticket is always read
    whole - nothing ever asks for message four - and inlining makes opening one a single
    round trip. The trade is a document-size ceiling, which a support conversation reaches
    only if something has already gone very wrong.

    `first_response_at` and `resolved_at` are stamped once and never recomputed, because they
    are what any response-time measurement is built on and a derived version would change
    every time somebody edited the thread.
    """
    subject: str
    raised_by: int

    program: str = "LMS"
    category: TicketCategory = TicketCategory.OTHER
    priority: TicketPriority = TicketPriority.NORMAL
    status: TicketStatus = TicketStatus.OPEN

    # [{"author_id", "author_name", "author_role", "body", "attachments", "is_internal",
    #   "sent_at"}]
    # `is_internal` marks a note between staff that the reporter never sees, which is what
    # stops the team either talking in a second system or in front of the customer.
    messages: list[dict[str, Any]] = field(default_factory=list)

    assigned_to: int | None = None
    # Set when the ticket concerns a specific student - a parent asking about their child's
    # marks - so support can open the right record without asking.
    about_student_id: int | None = None

    first_response_at: datetime | None = None
    resolved_at: datetime | None = None
    closed_at: datetime | None = None
    resolution_note: str | None = None
    # 1-5, asked for after resolution. Null means never answered, which is most of them.
    satisfaction_rating: int | None = None

    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class HomeworkAssignment:
    """
    Work set for a class, due by a date.

    Separate from the exam module even though both are "work a student hands in", because
    they answer to different rules: an exam has a window, an answer key and a valuation
    process, while homework has a due date and a tick. Modelling homework as a one-question
    exam would drag every exam rule - windows, grace periods, submission states - into a
    teacher setting five sums for tomorrow.

    The brief asks for daily homework and a weekly exam, and `is_mandatory` is how the
    cadence check tells the difference between work that counts towards that obligation and
    an optional revision sheet.
    """
    class_id: int
    subject_id: int
    teacher_id: int
    title: str
    due_date: date

    description: str | None = None
    assigned_date: date = field(default_factory=date.today)
    max_marks: float | None = None
    is_mandatory: bool = True
    allow_late_submission: bool = True

    # [{"file_name", "file_url", "content_type"}]
    attachments: list[dict[str, Any]] = field(default_factory=list)

    program: str = "LMS"
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class HomeworkSubmission:
    """
    One student's answer to one assignment.

    The document id is derived from both (`{assignment_id}:{student_id}`), exactly as exam
    submissions are, so a student pressing submit twice updates their work rather than
    filing a second copy.

    MISSED is reached by the clock, not by a teacher: the daily-homework report has to be
    answerable the morning after, and one that waits for marking measures how busy the
    teacher is rather than whether the work was done.
    """
    assignment_id: int
    student_id: int

    status: HomeworkStatus = HomeworkStatus.ASSIGNED
    body: str | None = None
    attachments: list[dict[str, Any]] = field(default_factory=list)

    submitted_at: datetime | None = None
    is_late: bool = False

    marks: float | None = None
    feedback: str | None = None
    graded_by: int | None = None
    graded_at: datetime | None = None

    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    id: str | None = None
