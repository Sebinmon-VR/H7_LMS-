"""
Online tuition records.

The tuition product is one-to-one: for one subject, one student has exactly one teacher, and
nobody else is in the room. That single sentence drives every shape in this file and is worth
holding onto, because it is what makes tuition genuinely different from the LMS rather than a
skin on it:

  * There is no roster. The LMS asks "who is in 7A?"; tuition asks "who teaches Priya
    physics?", and the answer is one person.
  * A timetable clash is personal, not institutional. Two LMS periods clash when they need
    the same teacher or the same room. Two tuition slots clash when they need the same
    teacher *or the same student* - a student with maths and physics at 17:00 on a Tuesday
    is as broken as a double-booked teacher, and only one of those is a problem the LMS has.
  * Billing is by the class. The admin's reports count conducted sessions, so a session's
    status is financial data, not just a display state.

As elsewhere in this codebase these dataclasses are descriptive: the routers read and write
plain dicts, and these are the reference for what is actually stored.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app.core.enums import (
    AttendanceStatus, DayOfWeek, FeeBasis, InvoiceStatus, LibraryApprovalStatus,
    LibraryVisibility, TuitionEnrollmentStatus, TuitionSessionStatus,
)


@dataclass(slots=True)
class TuitionEnrollment:
    """
    One student taking one subject with one teacher.

    The spine of the product. Slots, sessions, materials, assignments and invoices all hang
    off this, and the pairing is enforced as unique on (student, subject) while active: a
    second teacher for the same subject is not a richer arrangement, it is a data error that
    would make "who marks this?" unanswerable.

    `syllabus` and `goals` are free text rather than a structured curriculum. Private tuition
    is bought for a reason that rarely matches a syllabus document - "board exam in March",
    "catch up on trigonometry" - and forcing that into topic rows would lose it.
    """
    student_id: int
    subject_id: int
    teacher_id: int

    status: TuitionEnrollmentStatus = TuitionEnrollmentStatus.ACTIVE
    # What the student is being taught, and why. Shown to teacher and student alike.
    syllabus: str | None = None
    goals: str | None = None
    grade_level: str | None = None
    # Default length for classes on this arrangement; falls back to the programme default.
    default_duration_minutes: int | None = None
    # Overrides the fee plan resolved from the subject, when this student's rate differs.
    fee_plan_id: int | None = None

    start_date: date | None = None
    end_date: date | None = None
    notes: str | None = None

    created_by: int | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class TuitionSlot:
    """
    A recurring weekly class time on one enrollment.

    Stored as a rule ("Tuesdays 17:00, 60 minutes, from 1 Sep") rather than as events, for the
    same reason the LMS timetable is: a term is a handful of rules, not two hundred rows.
    Times are wall-clock in the *programme* timezone, and each user sees them converted into
    their own - see `app.services.tuition.common`.

    `student_id`, `teacher_id` and `subject_id` are copied down from the enrollment rather
    than resolved through it. Conflict detection has to read every slot belonging to a person
    on every write, and doing that through a join would turn one query into one-per-slot.
    They are rewritten whenever the enrollment's teacher changes.
    """
    enrollment_id: int
    student_id: int
    teacher_id: int
    subject_id: int

    day_of_week: DayOfWeek
    start_time: str = "17:00"          # "HH:MM", programme-local wall clock.
    duration_minutes: int = 60
    # Derived from start_time + duration on write, so overlap checks compare two stored
    # strings instead of recomputing arithmetic on every candidate pair.
    end_time: str = "18:00"

    effective_from: date | None = None
    effective_to: date | None = None
    is_active: bool = True
    # Where the class meets. A slot may carry a standing link (a teacher's permanent Meet
    # room); when it does not, each session gets its own.
    meeting_link: str | None = None
    auto_create_meet: bool = True

    created_by: int | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class TuitionSession:
    """
    One class, on one date. The unit of attendance and of billing.

    ## The timing rules

    These are the heart of the module and the part most worth reading twice. A session has a
    fixed length, set by the admin. From that follow three stored instants and one derived
    one:

      * `scheduled_start_at` / `scheduled_end_at` - what the timetable promised.
      * `teacher_joined_at` - when the teacher actually arrived.
      * `student_joined_at` - when the student actually arrived.
      * `effective_end_at` - when the class may actually be stopped.

    `effective_end_at` answers the requirement directly. If the teacher joins late, the
    student is owed their full lesson, so the end moves out to `teacher_joined_at + duration`
    - capped, because a teacher an hour late cannot push a class into the slot behind it. If
    the *student* joins late, nothing moves: the teacher may stop at the end of the period,
    and the minutes the student missed are the student's. The asymmetry is deliberate and is
    the whole point - late is not late in the same way for the two sides of a paid lesson.

    Neither party's lateness is inferred from the other's. `teacher_joined_at` is set when
    the teacher starts the class and `student_joined_at` when the student joins; a session
    where neither happened stays SCHEDULED until the sweep marks it a no-show.

    ## Attendance

    There is no separate attendance collection. With one student in the room, attendance *is*
    the session outcome, and a second document keyed by the same pair would only be a place
    for the two to disagree.
    """
    enrollment_id: int
    slot_id: int | None
    student_id: int
    teacher_id: int
    subject_id: int

    session_date: date = field(default_factory=date.today)
    # Absolute UTC instants. Unlike a slot's wall-clock rule, a session is a real appointment
    # two people in possibly different timezones have to be at simultaneously, so it is
    # stored as a moment rather than as a local time.
    scheduled_start_at: datetime | None = None
    scheduled_end_at: datetime | None = None
    duration_minutes: int = 60

    status: TuitionSessionStatus = TuitionSessionStatus.SCHEDULED
    title: str | None = None
    topic: str | None = None            # What was actually taught, filled in afterwards.
    teacher_notes: str | None = None

    # --- Who turned up, and when -------------------------------------------------------
    teacher_joined_at: datetime | None = None
    student_joined_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    # Recomputed whenever a join is recorded; see the class docstring.
    effective_end_at: datetime | None = None
    teacher_late_minutes: float = 0.0
    student_late_minutes: float = 0.0
    # Minutes the class was extended to make up for the teacher's late start. Surfaced
    # because it is the visible consequence of the rule and the first thing anybody queries
    # when they disagree about it.
    extension_minutes: float = 0.0
    # Minutes actually taught, from the earlier of the two arrivals to the end. What HOURLY
    # fee plans bill against.
    actual_duration_minutes: float | None = None

    # --- Attendance --------------------------------------------------------------------
    attendance_status: AttendanceStatus | None = None
    attendance_remarks: str | None = None
    attendance_marked_by: int | None = None
    attendance_marked_at: datetime | None = None

    # --- The meeting -------------------------------------------------------------------
    meeting_link: str | None = None
    # MANUAL when a link was pasted in, CREATED when this system made a Meet, FAILED when it
    # tried and could not. The session is always saved either way.
    meet_status: str | None = None
    meet_error: str | None = None
    google_event_id: str | None = None
    google_calendar_id: str | None = None
    recording_url: str | None = None

    # --- Rescheduling and cancellation --------------------------------------------------
    cancelled_by: int | None = None
    cancellation_reason: str | None = None
    rescheduled_from: datetime | None = None
    # True when an admin or teacher created this class by hand rather than the sweep
    # generating it from a slot. Ad-hoc classes survive a slot being edited or deleted.
    is_ad_hoc: bool = False
    # Whether this class counts towards the bill. Defaults from the status but can be
    # overridden by an admin, which is how a goodwill free class is recorded.
    is_billable: bool | None = None

    created_by: int | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    # Derived from (slot, date) for generated sessions so the sweep is idempotent: running it
    # twice updates the same document instead of double-booking the day.
    id: str | None = None


@dataclass(slots=True)
class TuitionLibraryItem:
    """
    A book, note, recording or link shared inside the tuition programme.

    Uploadable by all three roles, which is why `visibility` and `approval_status` are both
    here. Visibility is the uploader's intent; approval is whether that intent has been
    honoured yet. A student sharing their notes with the whole programme is a feature; a
    student sharing them with the whole programme *before anyone has looked at them* is a
    moderation problem, and separating the two fields is what lets the first happen without
    the second.
    """
    title: str
    uploaded_by: int
    uploader_role: str

    subject_id: int | None = None
    enrollment_id: int | None = None
    # Named individuals this reaches regardless of `visibility`, for a teacher handing one
    # specific book to one specific student.
    shared_with_user_ids: list[int] = field(default_factory=list)

    visibility: LibraryVisibility = LibraryVisibility.ENROLLMENT
    approval_status: LibraryApprovalStatus = LibraryApprovalStatus.APPROVED
    approved_by: int | None = None
    approved_at: datetime | None = None
    rejection_reason: str | None = None

    description: str | None = None
    material_type: str = "NOTES"       # NOTES | BOOK | RECORDING | LINK | WORKSHEET
    file_url: str | None = None
    external_url: str | None = None
    storage_provider: str | None = None
    storage_warning: str | None = None
    file_name: str | None = None
    file_size_bytes: int | None = None
    tags: list[str] = field(default_factory=list)

    download_count: int = 0
    uploaded_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class TuitionFeePlan:
    """
    What a tuition class costs.

    Resolved most-specific-first: the enrollment's own plan, then a plan for the subject,
    then the programme default. Admin-only in every direction - teachers and students never
    read this collection, which is why fee data lives here rather than on the enrollment.
    """
    name: str
    basis: FeeBasis = FeeBasis.PER_SESSION
    amount: float = 0.0
    currency: str = "AED"
    # Set to scope a plan to one subject; null makes it a programme-wide default.
    subject_id: int | None = None
    # Charged for a class the student missed without notice. Null bills the full amount.
    no_show_amount: float | None = None
    # Whether a class the *teacher* missed is billable. Almost never true; present so the
    # answer is recorded rather than assumed.
    charge_teacher_no_show: bool = False
    is_active: bool = True
    notes: str | None = None

    created_by: int | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class TuitionInvoice:
    """
    A student's bill for a period, built from counted sessions.

    `line_items` carries one row per enrollment with the session count it was priced from, so
    an invoice can always be explained back to the classes that produced it. A DRAFT is
    recomputed on every regeneration; once ISSUED the numbers are frozen, because a bill that
    silently changes after it was sent is not a bill.
    """
    student_id: int
    period_start: date
    period_end: date

    status: InvoiceStatus = InvoiceStatus.DRAFT
    currency: str = "AED"
    # [{"enrollment_id", "subject_id", "subject_name", "teacher_id", "sessions_counted",
    #   "sessions_attended", "sessions_missed", "basis", "unit_amount", "amount"}]
    line_items: list[dict[str, Any]] = field(default_factory=list)
    subtotal: float = 0.0
    discount_amount: float = 0.0
    tax_amount: float = 0.0
    total_amount: float = 0.0
    amount_paid: float = 0.0
    # [{"amount", "paid_at", "method", "reference", "recorded_by"}]
    payments: list[dict[str, Any]] = field(default_factory=list)

    issued_at: datetime | None = None
    due_date: date | None = None
    notes: str | None = None

    generated_by: int | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    id: int | None = None
