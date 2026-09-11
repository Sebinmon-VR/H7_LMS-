"""
Request and response shapes for the online tuition module.

Two conventions run through this file and are worth stating once rather than repeating in
thirty docstrings.

**Participants are never taken from the request.** A slot, a session and an assessment all
name an `enrollment_id`, and the student, teacher and subject are read from that arrangement
on the server. A caller therefore cannot schedule a class for a student they do not teach, or
set homework for somebody else's child, because the ids never come from them.

**Times come back twice.** Every instant is returned as stored - UTC - and again as
`<field>_local`, rendered in the reader's own timezone, with `viewer_timezone` naming which
zone that was. The brief asks that users see their own time; doing it additively means a
client that formats timestamps itself is unaffected, and one that would rather be handed the
answer just reads the other field.
"""

from datetime import date, datetime, time
from typing import Any, List

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.enums import (
    AttendanceStatus, DayOfWeek, ExamMode, ExamStatus, FeeBasis, GradingScheme,
    InvoiceStatus, LibraryVisibility, Program, TuitionEnrollmentStatus, TuitionSessionStatus,
)
from app.schemas.academic import SubjectOut
from app.schemas.user import UserProfileFields


# ---------------------------------------------------------------------------------------
# Program access
# ---------------------------------------------------------------------------------------

class ProgramAccessUpdate(BaseModel):
    """
    Which products a user may reach. `PUT /admin/tuition/users/{user_id}/programs`.

    This is the "special key" the brief describes: a teacher or student added to the tuition
    programme carries TUITION here, and every tuition endpoint checks it after the role
    guard. A school account without it is refused, however valid its login.
    """
    programs: List[Program] = Field(
        ..., min_length=1,
        description="Products this account may use. Replaces the current list outright.",
    )


class TuitionStudentCreate(UserProfileFields):
    """
    Adds a new student to the tuition programme. `POST /admin/tuition/students`.

    A fresh account, not a school pupil borrowed from the LMS. The two user bases overlap
    only sometimes - a tuition student may attend the school here, or may never have set foot
    in it - so this creates its own account with tuition access only. An administrator who
    genuinely wants one person in both grants LMS afterwards with
    `PUT /admin/tuition/users/{id}/programs`.

    `email` may be omitted and is derived from the name; `password` may be omitted and issued
    later with `POST /admin/users/{id}/generate-credentials`, which mails it. Every profile
    field is optional except the name, because an admin onboarding thirty students has
    partial data for most of them.
    """
    full_name: str = Field(..., min_length=1, max_length=150)
    email: str | None = Field(None, description="Derived from the name when omitted")
    password: str | None = Field(None, description="Issue one later if omitted")
    admission_number: str | None = Field(
        None, max_length=50,
        description="The student's unique id. Generated automatically when omitted.",
    )
    timezone: str | None = Field(
        None, max_length=64,
        description="IANA zone, e.g. 'Asia/Kolkata'. Left unset, it is detected from the "
                    "student's browser on their first request and saved.",
    )
    also_lms: bool = Field(
        False,
        description="Also grant school LMS access. Off by default - a tuition account that "
                    "silently reached the school's classes and marks would be a privacy "
                    "problem, not a convenience.",
    )

    @field_validator("full_name", mode="before")
    @classmethod
    def _strip_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class TuitionTeacherCreate(UserProfileFields):
    """
    Adds a new teacher to the tuition programme. `POST /admin/tuition/teachers`.

    Same reasoning as `TuitionStudentCreate`: a tuition teacher is not necessarily one of the
    school's staff, so this creates its own account rather than requiring an LMS profile to
    exist first.
    """
    full_name: str = Field(..., min_length=1, max_length=150)
    email: str | None = Field(None, description="Derived from the name when omitted")
    password: str | None = Field(None, description="Issue one later if omitted")
    employee_id: str | None = Field(
        None, max_length=50, description="Staff id. Generated automatically when omitted."
    )
    timezone: str | None = Field(
        None, max_length=64, description="IANA zone. Detected from the browser when unset."
    )
    subject_ids: List[int] = Field(
        default_factory=list,
        description="Subjects this teacher can take, recorded on the profile so the "
                    "enrollment screen can filter candidates. Does not itself assign anyone.",
    )
    also_lms: bool = Field(False, description="Also grant school LMS access")

    @field_validator("full_name", mode="before")
    @classmethod
    def _strip_name(cls, value):
        return value.strip() if isinstance(value, str) else value


class TuitionUserSummary(BaseModel):
    """A participant, trimmed to what a tuition screen needs to show and pick from."""
    id: int
    full_name: str
    email: str
    role: str
    is_active: bool = True
    programs: List[str] = Field(default_factory=lambda: ["LMS"])
    admission_number: str | None = None
    employee_id: str | None = None
    phone: str | None = None
    timezone: str | None = Field(None, description="Explicitly chosen zone, if any")
    detected_timezone: str | None = Field(
        None, description="Where this person's browser last reported being"
    )
    subject_ids: List[int] = Field(
        default_factory=list, description="Subjects a teacher can take"
    )
    photo_url: str | None = None

    model_config = ConfigDict(from_attributes=True, extra="ignore")


# ---------------------------------------------------------------------------------------
# Enrollments
# ---------------------------------------------------------------------------------------

class TuitionEnrollmentCreate(BaseModel):
    """
    Assigns a teacher to a student for one subject. `POST /admin/tuition/enrollments`.

    Rejected with 409 when the student already has an active teacher for that subject. One
    subject has one teacher - see `app.models.tuition.TuitionEnrollment` for why that is a
    rule rather than a preference.
    """
    student_id: int
    subject_id: int
    teacher_id: int
    syllabus: str | None = Field(None, max_length=5000, description="What is being taught")
    goals: str | None = Field(None, max_length=2000, description="Why, e.g. 'board exam in March'")
    grade_level: str | None = Field(None, max_length=50, description="e.g. 'Grade 10', 'A-Level'")
    default_duration_minutes: int | None = Field(
        None, ge=10, le=480, description="Class length for this arrangement. Falls back to "
                                         "the programme default."
    )
    fee_plan_id: int | None = Field(None, description="Overrides the subject's fee plan")
    start_date: date | None = None
    end_date: date | None = None
    notes: str | None = Field(None, max_length=2000)


class TuitionEnrollmentUpdate(BaseModel):
    """Partial update. Changing `teacher_id` re-points the slots and every class still to come."""
    teacher_id: int | None = None
    status: TuitionEnrollmentStatus | None = None
    syllabus: str | None = Field(None, max_length=5000)
    goals: str | None = Field(None, max_length=2000)
    grade_level: str | None = Field(None, max_length=50)
    default_duration_minutes: int | None = Field(None, ge=10, le=480)
    fee_plan_id: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    notes: str | None = Field(None, max_length=2000)


class TuitionEnrollmentOut(BaseModel):
    id: int
    student_id: int
    teacher_id: int
    subject_id: int
    status: str
    student: TuitionUserSummary | None = None
    teacher: TuitionUserSummary | None = None
    subject: SubjectOut | None = None

    syllabus: str | None = None
    goals: str | None = None
    grade_level: str | None = None
    default_duration_minutes: int | None = None
    fee_plan_id: int | None = None
    start_date: date | None = None
    end_date: date | None = None
    notes: str | None = None
    created_by: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True, extra="ignore")


# ---------------------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------------------

class TuitionSlotCreate(BaseModel):
    """
    A recurring weekly class time. `POST /admin/tuition/slots`.

    Refused with 409 and a list of conflicts when either the student or the teacher is
    already committed at that time - checked from both diaries, because two tuition teachers
    have no other way of knowing about each other.
    """
    enrollment_id: int
    day_of_week: DayOfWeek
    start_time: time = Field(..., description="Local start time in programme timezone, e.g. 17:00")
    duration_minutes: int | None = Field(
        None, ge=10, le=480,
        description="Falls back to the enrollment's default, then the programme default.",
    )
    effective_from: date | None = Field(None, description="First date this applies. Defaults to today.")
    effective_to: date | None = Field(None, description="Last date. Open-ended when omitted.")
    is_active: bool = True
    meeting_link: str | None = Field(
        None, max_length=2048,
        description="A standing link for this slot - Meet, Zoom, Teams. When omitted each "
                    "class gets its own Google Meet on demand.",
    )
    auto_create_meet: bool = True

    @model_validator(mode="after")
    def _check_dates(self):
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("effective_to cannot be earlier than effective_from")
        return self


class TuitionSlotUpdate(BaseModel):
    day_of_week: DayOfWeek | None = None
    start_time: time | None = None
    duration_minutes: int | None = Field(None, ge=10, le=480)
    effective_from: date | None = None
    effective_to: date | None = None
    is_active: bool | None = None
    meeting_link: str | None = Field(None, max_length=2048)
    auto_create_meet: bool | None = None


class TuitionSlotOut(BaseModel):
    id: int
    enrollment_id: int
    student_id: int
    teacher_id: int
    subject_id: int
    day_of_week: str
    start_time: str
    end_time: str
    duration_minutes: int
    effective_from: date | None = None
    effective_to: date | None = None
    is_active: bool = True
    meeting_link: str | None = None
    auto_create_meet: bool = True
    timezone: str | None = None

    student: TuitionUserSummary | None = None
    teacher: TuitionUserSummary | None = None
    subject: SubjectOut | None = None

    # Present on a create or update response. Non-empty with `allow_conflicts` means the
    # clash was deliberately overridden and is recorded rather than hidden.
    conflicts: List[str] = Field(default_factory=list)
    sessions_generated: int | None = None
    sessions_removed: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True, extra="ignore")


class SlotAvailabilityQuery(BaseModel):
    """
    'Can I put a class here?' - asked before the admin commits to a time.

    Exists so a scheduling UI can grey out impossible slots rather than letting somebody fill
    in a form and rejecting it at the end.
    """
    enrollment_id: int
    day_of_week: DayOfWeek
    start_time: time
    duration_minutes: int = Field(60, ge=10, le=480)
    effective_from: date | None = None
    effective_to: date | None = None
    exclude_slot_id: int | None = Field(None, description="Ignore this slot - use when editing it")


class SlotAvailabilityResult(BaseModel):
    available: bool
    conflicts: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------------------

class TuitionSessionCreate(BaseModel):
    """A one-off extra class outside the weekly pattern. Revision, or a catch-up."""
    enrollment_id: int
    scheduled_start_at: datetime = Field(..., description="Absolute start. Send with a timezone offset.")
    duration_minutes: int | None = Field(None, ge=10, le=480)
    title: str | None = Field(None, max_length=200)
    meeting_link: str | None = Field(None, max_length=2048)
    auto_create_meet: bool = True


class TuitionSessionReschedule(BaseModel):
    """Moves one class without touching the recurring slot behind it."""
    scheduled_start_at: datetime
    duration_minutes: int | None = Field(None, ge=10, le=480)
    reason: str | None = Field(None, max_length=500)


class TuitionSessionCancel(BaseModel):
    reason: str | None = Field(None, max_length=500)


class TuitionSessionEnd(BaseModel):
    """
    Closes a class.

    Allowed before `effective_end_at` - connections drop and students leave - but an early
    finish is recorded as `ended_early` and `short_by_minutes`, which is what an admin's
    report reads. See `app.services.tuition.sessions` for why that is recorded rather than
    refused.
    """
    topic: str | None = Field(None, max_length=500, description="What was actually taught")
    notes: str | None = Field(None, max_length=5000)
    recording_url: str | None = Field(None, max_length=2048)


class TuitionAttendanceMark(BaseModel):
    """
    The teacher's record of whether the student attended.

    Overrides whatever the join timestamps imply: a student whose connection failed and who
    phoned in is present, however the log reads.
    """
    status: AttendanceStatus
    remarks: str | None = Field(None, max_length=500)


class MeetingLinkUpdate(BaseModel):
    """Replaces a class's link with one from another provider - Zoom, Teams, a standing room."""
    meeting_link: str = Field(..., max_length=2048)


class SessionTiming(BaseModel):
    """
    The countdown, resolved on the server.

    Returned rather than left to the client to derive, so the teacher's screen and the
    student's screen cannot disagree about when the class ends - which, given that one of
    them is entitled to stop and the other is entitled to stay, is exactly the disagreement
    worth designing out.
    """
    now: datetime
    starts_in_minutes: float | None = None
    minutes_remaining: float | None = None
    effective_end_at: datetime | None = None
    extension_minutes: float = 0.0
    teacher_late_minutes: float = 0.0
    student_late_minutes: float = 0.0
    may_end_now: bool = False
    is_live: bool = False

    # Has the class actually begun? Distinct from `is_live`, which reflects the stored
    # status: this is derived from the clock, so a client is never told the class has not
    # started when it plainly has because a background sweep is a minute behind.
    auto_start: bool = False
    class_started_at: datetime | None = Field(
        None, description="When the class actually began - what student lateness is measured "
                          "from. Null while it has not started."
    )
    class_has_started: bool = False
    waiting_for_teacher: bool = Field(
        False,
        description="The scheduled time has passed but the teacher has not started the "
                    "class. The student is waiting, not late - show this, not a late warning.",
    )


class TuitionSessionOut(BaseModel):
    id: str | int
    enrollment_id: int
    slot_id: int | None = None
    student_id: int
    teacher_id: int
    subject_id: int

    session_date: date | None = None
    scheduled_start_at: datetime | None = None
    scheduled_end_at: datetime | None = None
    effective_end_at: datetime | None = None
    duration_minutes: int = 60
    status: str = TuitionSessionStatus.SCHEDULED.value
    title: str | None = None
    topic: str | None = None
    teacher_notes: str | None = None

    teacher_joined_at: datetime | None = None
    student_joined_at: datetime | None = None
    started_at: datetime | None = None
    class_started_at: datetime | None = Field(
        None, description="When the class began. Student lateness is measured from this, "
                          "not from the scheduled start."
    )
    auto_started: bool | None = Field(
        None, description="True when the class opened on its timetable rather than because "
                          "the teacher pressed start."
    )
    ended_at: datetime | None = None
    teacher_late_minutes: float = 0.0
    student_late_minutes: float = 0.0
    extension_minutes: float = 0.0
    actual_duration_minutes: float | None = None
    ended_early: bool | None = None
    short_by_minutes: float | None = None

    attendance_status: str | None = None
    attendance_remarks: str | None = None
    attendance_marked_by: int | None = None
    attendance_marked_at: datetime | None = None
    suggested_attendance: str | None = Field(
        None, description="What the join times imply, offered as a default for the teacher."
    )

    meeting_link: str | None = None
    meet_status: str | None = None
    meet_error: str | None = None
    recording_url: str | None = None

    is_ad_hoc: bool = False
    is_billable: bool | None = None
    cancelled_by: int | None = None
    cancellation_reason: str | None = None
    rescheduled_from: datetime | None = None

    student: TuitionUserSummary | None = None
    teacher: TuitionUserSummary | None = None
    subject: SubjectOut | None = None
    timing: SessionTiming | None = None
    conflicts: List[str] = Field(default_factory=list)

    # Local renderings for the reader's own zone; see the module docstring.
    scheduled_start_at_local: datetime | None = None
    scheduled_end_at_local: datetime | None = None
    effective_end_at_local: datetime | None = None
    teacher_joined_at_local: datetime | None = None
    student_joined_at_local: datetime | None = None
    started_at_local: datetime | None = None
    class_started_at_local: datetime | None = None
    ended_at_local: datetime | None = None
    viewer_timezone: str | None = None

    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True, extra="ignore")


# ---------------------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------------------

class LibraryItemBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    material_type: str = Field("NOTES", description="NOTES | BOOK | RECORDING | LINK | WORKSHEET | QUESTION_PAPER")
    subject_id: int | None = None
    enrollment_id: int | None = Field(
        None, description="Scopes the item to one arrangement. Required for ENROLLMENT visibility."
    )
    visibility: LibraryVisibility = LibraryVisibility.ENROLLMENT
    shared_with_user_ids: List[int] = Field(
        default_factory=list, description="Named individuals this reaches regardless of visibility"
    )
    tags: List[str] = Field(default_factory=list, max_length=20)


class LibraryLinkCreate(LibraryItemBase):
    """
    Shares a link rather than a file - a publisher's page, a video, a Drive folder.

    Better than uploading a copy of something already on the web: a copy goes stale, costs
    storage, and loses the source.
    """
    external_url: str = Field(..., max_length=2048)


class LibraryItemUpdate(BaseModel):
    """
    Partial update.

    Widening an approved *student* upload's visibility re-opens it for review. Without that,
    the approval step would be bypassed by uploading narrow and editing wide.
    """
    title: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    material_type: str | None = None
    subject_id: int | None = None
    visibility: LibraryVisibility | None = None
    shared_with_user_ids: List[int] | None = None
    tags: List[str] | None = None
    external_url: str | None = Field(None, max_length=2048)


class LibraryModeration(BaseModel):
    approve: bool
    reason: str | None = Field(None, max_length=500, description="Required in practice when rejecting")


class LibraryItemOut(BaseModel):
    id: int
    title: str
    description: str | None = None
    material_type: str = "NOTES"
    subject_id: int | None = None
    enrollment_id: int | None = None
    visibility: str
    approval_status: str
    approved_by: int | None = None
    approved_at: datetime | None = None
    rejection_reason: str | None = None

    file_url: str | None = None
    external_url: str | None = None
    file_name: str | None = None
    storage_provider: str | None = None
    storage_warning: str | None = None
    tags: List[str] = Field(default_factory=list)
    shared_with_user_ids: List[int] = Field(default_factory=list)

    uploaded_by: int
    uploader_role: str | None = None
    uploader: TuitionUserSummary | None = None
    subject: SubjectOut | None = None
    download_count: int = 0
    uploaded_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True, extra="ignore")


# ---------------------------------------------------------------------------------------
# Assessments
# ---------------------------------------------------------------------------------------

class GradeBandIn(BaseModel):
    grade: str = Field(..., max_length=10)
    min_percentage: float = Field(..., ge=0, le=100)


class TuitionAssessmentCreate(BaseModel):
    """
    Homework, an assignment or an exam, set for one student.

    Everything except `enrollment_id` and `category` is the LMS exam form unchanged, because
    the engine behind it is the LMS exam engine unchanged - question types, answer keys,
    timed windows, auto-marking, valuation and publishing all work here exactly as they do
    for a class.
    """
    enrollment_id: int
    category: str = Field("HOMEWORK", description="HOMEWORK | ASSIGNMENT | EXAM | TEST | PROJECT")
    title: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    instructions: str | None = Field(None, max_length=5000)
    mode: ExamMode = ExamMode.ONLINE
    status: ExamStatus = ExamStatus.DRAFT

    starts_at: datetime
    ends_at: datetime
    duration_minutes: int | None = Field(None, ge=1, le=1440)
    upload_grace_minutes: int = Field(0, ge=0, le=1440)
    late_submission_allowed: bool = True

    grading_scheme: GradingScheme = GradingScheme.MARKS
    max_marks: float | None = Field(None, gt=0, description="Defaults to the questions' total")
    pass_marks: float | None = Field(None, ge=0)
    grade_bands: List[GradeBandIn] = Field(default_factory=list)

    questions: List[Any] = Field(default_factory=list)
    shuffle_questions: bool = False
    auto_grade_objective: bool = True
    max_upload_files: int = Field(5, ge=1, le=20)

    @model_validator(mode="after")
    def _check_window(self):
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be later than starts_at")
        return self


class TuitionReportCardCreate(BaseModel):
    """
    Consolidates a student's tuition work onto one card.

    No rank and no class size: a one-to-one student has no cohort, and ranking them against
    other people's private students would be meaningless as well as intrusive.
    """
    student_id: int
    title: str = Field(..., min_length=1, max_length=150, description="e.g. 'Term 1 2026'")
    from_date: date | None = None
    to_date: date | None = None
    count_missing_as_zero: bool = Field(
        False,
        description="Average unsat papers in as zero. Off by default - an absence is not a "
                    "mark, and averaging one in without being told to misreports the student.",
    )
    remarks: str | None = Field(None, max_length=2000)


# ---------------------------------------------------------------------------------------
# Fees
# ---------------------------------------------------------------------------------------

class FeePlanCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    basis: FeeBasis = FeeBasis.PER_SESSION
    amount: float = Field(..., ge=0)
    currency: str | None = Field(None, max_length=8)
    subject_id: int | None = Field(None, description="Scopes to one subject; null is the default plan")
    no_show_amount: float | None = Field(
        None, ge=0, description="Charged for a class the student missed. Null bills in full."
    )
    charge_teacher_no_show: bool = False
    is_active: bool = True
    notes: str | None = Field(None, max_length=1000)


class FeePlanUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=150)
    basis: FeeBasis | None = None
    amount: float | None = Field(None, ge=0)
    currency: str | None = Field(None, max_length=8)
    subject_id: int | None = None
    no_show_amount: float | None = Field(None, ge=0)
    charge_teacher_no_show: bool | None = None
    is_active: bool | None = None
    notes: str | None = Field(None, max_length=1000)


class FeePlanOut(BaseModel):
    id: int
    name: str
    basis: str
    amount: float
    currency: str
    subject_id: int | None = None
    no_show_amount: float | None = None
    charge_teacher_no_show: bool = False
    is_active: bool = True
    notes: str | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True, extra="ignore")


class InvoiceGenerate(BaseModel):
    """
    Builds a bill from counted classes. Refuses to overwrite one that has already been issued.
    """
    student_id: int
    period_start: date
    period_end: date
    discount_amount: float = Field(0.0, ge=0)
    tax_amount: float = Field(0.0, ge=0)
    due_date: date | None = None
    notes: str | None = Field(None, max_length=1000)

    @model_validator(mode="after")
    def _check_period(self):
        if self.period_end < self.period_start:
            raise ValueError("period_end cannot be earlier than period_start")
        return self


class InvoiceBatchGenerate(BaseModel):
    """Bills everyone with classes in a period. Already-issued invoices are skipped, not failed."""
    period_start: date
    period_end: date


class PaymentRecord(BaseModel):
    amount: float = Field(..., gt=0)
    method: str | None = Field(None, max_length=50, description="e.g. Bank transfer, Cash")
    reference: str | None = Field(None, max_length=100)
    paid_at: datetime | None = None


class InvoiceOut(BaseModel):
    id: str
    student_id: int
    student_name: str | None = None
    admission_number: str | None = None
    period_start: date
    period_end: date
    status: str = InvoiceStatus.DRAFT.value
    currency: str
    line_items: List[dict] = Field(default_factory=list)
    subtotal: float = 0.0
    discount_amount: float = 0.0
    tax_amount: float = 0.0
    total_amount: float = 0.0
    amount_paid: float = 0.0
    payments: List[dict] = Field(default_factory=list)
    issued_at: datetime | None = None
    due_date: date | None = None
    notes: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True, extra="ignore")


# ---------------------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------------------

class AttendanceTotals(BaseModel):
    """
    The counts every tuition report is built from.

    `attendance_percentage` is measured against classes actually **conducted**, not against
    classes scheduled. A student whose teacher missed two classes has 100% attendance, not
    60% - and since these counts price the invoices, the distinction is money as well as
    fairness.
    """
    total_sessions: int = 0
    conducted: int = 0
    attended: int = 0
    late: int = 0
    missed: int = 0
    cancelled: int = 0
    teacher_no_show: int = 0
    upcoming: int = 0
    billable_sessions: int = 0
    attendance_percentage: float | None = None
    scheduled_minutes: float = 0.0
    taught_minutes: float = 0.0


class StudentAttendanceReport(BaseModel):
    student_id: int
    student_name: str | None = None
    admission_number: str | None = None
    from_date: date
    to_date: date
    totals: AttendanceTotals
    subjects: List[dict] = Field(default_factory=list)


class TeacherAttendanceReport(BaseModel):
    teacher_id: int
    teacher_name: str | None = None
    employee_id: str | None = None
    from_date: date
    to_date: date
    totals: AttendanceTotals
    students: List[dict] = Field(default_factory=list)


class ProgrammeReport(BaseModel):
    from_date: date
    to_date: date
    totals: AttendanceTotals
    students: List[dict] = Field(default_factory=list)
    teachers: List[dict] = Field(default_factory=list)


# ---------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------

class ProgramSettingsUpdate(BaseModel):
    """
    Runtime configuration, editable by an admin for either product.

    The brief asks for the reminder lead time to be settable "for both the lms and tuition",
    and for the timezone to be settable at all - so both live here rather than in environment
    variables. Omitted fields are left alone: the admin UI may render only half of these, and
    a PUT that dropped the rest would reset behaviour nobody meant to touch.
    """
    timezone: str | None = Field(None, description="IANA zone, e.g. 'Asia/Dubai'")
    reminders_enabled: bool | None = None
    reminder_minutes_before: List[int] | None = Field(
        None, description="Minutes before a class to remind. Several values send several nudges."
    )
    remind_teachers: bool | None = None
    reminder_max_lateness_minutes: int | None = Field(None, ge=0, le=240)

    # Tuition only; ignored when the program is LMS.
    default_session_minutes: int | None = Field(None, ge=10, le=480)
    auto_start_class: bool | None = Field(
        None,
        description="False (default): the class begins when the teacher presses start, and a "
                    "student cannot be late for a class nobody has started. True: the class "
                    "opens on its timetabled slot regardless, and lateness is measured from "
                    "the timetable. Both join times are recorded either way, and a late "
                    "teacher still earns the student extra time.",
    )
    max_teacher_late_extension_minutes: int | None = Field(
        None, ge=0, le=240,
        description="Cap on how far a late teacher may push a class past its scheduled end.",
    )
    teacher_no_show_minutes: int | None = Field(None, ge=1, le=240)
    student_late_grace_minutes: int | None = Field(None, ge=0, le=120)
    min_gap_minutes: int | None = Field(
        None, ge=0, le=240, description="Minimum breathing room between one person's classes"
    )
    session_horizon_days: int | None = Field(None, ge=1, le=365)
    student_uploads_need_approval: bool | None = None
    currency: str | None = Field(None, max_length=8)
    default_session_fee: float | None = Field(None, ge=0)
    auto_create_meet: bool | None = None


class TimezoneUpdate(BaseModel):
    """
    A user's own display timezone. `PUT /tuition/me/timezone`.

    Self-service on purpose: the person who knows where a student is sitting is the student.
    An empty value clears it and puts them back on programme time.
    """
    timezone: str | None = Field(None, description="IANA zone, e.g. 'Europe/London'. Null clears it.")
