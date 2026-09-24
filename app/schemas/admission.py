"""
Request and response shapes for academic years and admission categories.
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.enums import (
    AcademicTerm, AcademicYearStatus, AdmissionRequestStatus, Gender, GuardianRelation, Program,
)
from app.schemas.user import CredentialsIssued, UserOut


class AcademicTermIn(BaseModel):
    """
    One half of the year. Given only when a school cuts its terms differently from the
    default April-October / November-March split.
    """
    key: AcademicTerm
    name: str | None = Field(None, max_length=50, description='e.g. "Term 1"')
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def _dates_in_order(self):
        if self.end_date < self.start_date:
            raise ValueError("A term's end_date must not fall before its start_date")
        return self


class AcademicTermOut(BaseModel):
    key: str
    name: str
    start_date: date
    end_date: date
    # Whether today falls inside this term - the one the frontend should preselect.
    is_current: bool = False


class AcademicYearCreate(BaseModel):
    """
    A session year. Runs April to March by default.

    `start_date` and `end_date` may be omitted: a name like "2026-27" or "2026" resolves to
    1 April 2026 - 31 March 2027, and any other name to the April-March year the clock is in.
    `terms` may be omitted too, in which case the year is cut into Term 1 (to the end of
    October) and Term 2 (November to the end).
    """
    name: str = Field(..., min_length=1, max_length=50, description='e.g. "2026-27"')
    start_date: date | None = Field(None, description="Defaults to 1 April of the named year.")
    end_date: date | None = Field(None, description="Defaults to 31 March of the following year.")
    terms: list[AcademicTermIn] | None = Field(
        None, description="Omit for Term 1 (April-October) and Term 2 (November-March)."
    )

    code: str | None = Field(None, max_length=20, description='e.g. "AY2526"')
    status: AcademicYearStatus = AcademicYearStatus.UPCOMING
    # Setting this clears the flag on every other year; see the service. Offered at creation
    # because the first year a deployment creates is almost always the current one, and
    # forcing a second call to say so is how a fresh install ends up with none.
    is_current: bool = False
    admissions_open: bool = True
    programs: list[Program] = Field(default_factory=lambda: [Program.LMS])
    notes: str | None = Field(None, max_length=2000)

    @model_validator(mode="after")
    def _dates_in_order(self):
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("Give both start_date and end_date, or neither.")
        if self.start_date and self.end_date and self.end_date <= self.start_date:
            raise ValueError("end_date must fall after start_date")
        return self


class AcademicYearUpdate(BaseModel):
    """
    Partial update; omitted fields are left unchanged.

    Changing the dates re-derives the terms unless `terms` is given in the same call, so a
    year moved to a different window does not keep term dates that fall outside it.
    """
    name: str | None = Field(None, min_length=1, max_length=50)
    start_date: date | None = None
    end_date: date | None = None
    terms: list[AcademicTermIn] | None = None
    code: str | None = Field(None, max_length=20)
    status: AcademicYearStatus | None = None
    is_current: bool | None = None
    admissions_open: bool | None = None
    programs: list[Program] | None = None
    notes: str | None = Field(None, max_length=2000)


class AcademicYearOut(BaseModel):
    id: int
    name: str
    code: str | None = None
    start_date: date
    end_date: date
    status: AcademicYearStatus
    is_current: bool
    admissions_open: bool
    programs: list[str] = Field(default_factory=list)
    terms: list[AcademicTermOut] = Field(default_factory=list)
    # The term today falls in, when the year covers today. What billing defaults to.
    current_term: str | None = None
    notes: str | None = None

    # Counts, so the admin list does not need a request per row to show how full a year is.
    student_count: int | None = None
    category_count: int | None = None

    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class AdmissionCategoryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description='e.g. "Staff Ward"')
    code: str = Field(..., min_length=1, max_length=30)
    description: str | None = Field(None, max_length=1000)
    academic_year_id: int | None = Field(
        None, description="Scope to one year; omit for a standing category."
    )
    programs: list[Program] = Field(default_factory=lambda: [Program.LMS])
    default_discount_percent: float | None = Field(None, ge=0, le=100)
    waives_admission_charge: bool = False
    is_active: bool = True
    sort_order: int = 0


class AdmissionCategoryUpdate(BaseModel):
    """Partial update; omitted fields are left unchanged."""
    name: str | None = Field(None, min_length=1, max_length=100)
    code: str | None = Field(None, min_length=1, max_length=30)
    description: str | None = Field(None, max_length=1000)
    academic_year_id: int | None = None
    programs: list[Program] | None = None
    default_discount_percent: float | None = Field(None, ge=0, le=100)
    waives_admission_charge: bool | None = None
    is_active: bool | None = None
    sort_order: int | None = None


class AdmissionCategoryOut(BaseModel):
    id: int
    name: str
    code: str
    description: str | None = None
    academic_year_id: int | None = None
    academic_year_name: str | None = None
    programs: list[str] = Field(default_factory=list)
    default_discount_percent: float | None = None
    waives_admission_charge: bool = False
    is_active: bool = True
    sort_order: int = 0
    student_count: int | None = None

    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------------------
# Mapping students into a year
# ---------------------------------------------------------------------------------------

class YearStudentOut(BaseModel):
    """One student on a year's roster."""
    student_id: int
    full_name: str | None = None
    email: str | None = None
    admission_number: str | None = None
    roll_number: str | None = None
    class_id: int | None = None
    class_name: str | None = None
    admission_category_id: int | None = None
    admission_category_name: str | None = None
    syllabus: str | None = None
    is_active: bool = True


class AssignStudentsRequest(BaseModel):
    student_ids: list[int] = Field(..., min_length=1, max_length=2000)
    dry_run: bool = Field(
        False, description="Report what would change without writing anything."
    )


class AssignedStudent(BaseModel):
    student_id: int
    full_name: str | None = None
    from_year_id: int | None = None


class SkippedStudent(BaseModel):
    student_id: int
    reason: str


class AssignStudentsResult(BaseModel):
    dry_run: bool
    academic_year_id: int
    academic_year_name: str | None = None
    moved: list[AssignedStudent] = Field(default_factory=list)
    moved_count: int = 0
    already_in_year: list[int] = Field(default_factory=list)
    # Ids that were not students, or did not exist. Reported rather than failing the call:
    # a roster pasted from a spreadsheet nearly always has one bad row, and rejecting all of
    # it teaches people to stop using the import.
    skipped: list[SkippedStudent] = Field(default_factory=list)
    detail: str


class PromoteRequest(BaseModel):
    """
    The July rollover.

    `class_map` is `{"from_class_id": to_class_id}`. A class absent from the map keeps its
    students where they are, so a partial map is safe. `graduating_class_ids` names the
    classes with no next year - those students are left in the source year for the office to
    deal with deliberately, rather than being moved or deactivated by a bulk operation.
    """
    source_year_id: int
    class_map: dict[int, int] = Field(
        default_factory=dict, description='e.g. {"7": 8, "8": 9} - class ids, not names.'
    )
    graduating_class_ids: list[int] = Field(default_factory=list)
    dry_run: bool = Field(
        True, description="Defaults to true. A rollover touches every student in the school."
    )


class PromotedStudent(BaseModel):
    student_id: int
    full_name: str | None = None
    admission_number: str | None = None
    from_class_id: int | None = None
    to_class_id: int | None = None
    class_changed: bool = False


class GraduatingStudent(BaseModel):
    student_id: int
    full_name: str | None = None
    class_id: int | None = None


class PromoteResult(BaseModel):
    dry_run: bool
    source_year_id: int
    source_year_name: str | None = None
    target_year_id: int
    target_year_name: str | None = None
    promoted: list[PromotedStudent] = Field(default_factory=list)
    promoted_count: int = 0
    class_changed_count: int = 0
    kept_same_class_count: int = 0
    graduating: list[GraduatingStudent] = Field(default_factory=list)
    graduating_count: int = 0
    detail: str


# ---------------------------------------------------------------------------------------
# Online admission requests
#
# The public website posts `AdmissionRequestCreate` with no login. Everything the office
# then does to it - review, waitlist, admit, decline - is admin-only and shaped below.
# ---------------------------------------------------------------------------------------

def _blank_to_none(value):
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return value


class AdmissionParentIn(BaseModel):
    """
    One parent or guardian on the form.

    Exactly one entry is the primary contact - the person the office writes to, and the one
    a parent login is created for on admission. The primary needs a phone number and an
    email address; the others may be a name alone.
    """
    relation: GuardianRelation = GuardianRelation.GUARDIAN
    full_name: str = Field(..., min_length=1, max_length=150)
    phone: str | None = Field(None, max_length=32)
    email: str | None = Field(None, max_length=320)
    occupation: str | None = Field(None, max_length=150)
    is_primary: bool = False

    @field_validator("full_name", "phone", "occupation", mode="before")
    @classmethod
    def _strip(cls, value):
        return _blank_to_none(value)

    @field_validator("email", mode="before")
    @classmethod
    def _lower_email(cls, value):
        cleaned = _blank_to_none(value)
        return cleaned.lower() if isinstance(cleaned, str) else cleaned

    @field_validator("email")
    @classmethod
    def _email_shape(cls, value):
        if value is not None and ("@" not in value or "." not in value.split("@")[-1]):
            raise ValueError("Enter a valid email address")
        return value


class AdmissionParentOut(AdmissionParentIn):
    pass


class AdmissionRequestCreate(BaseModel):
    """
    What the website form submits.

    Only the child's name and date of birth, the class asked for, one parent with a phone
    and email, and the declaration are required. Everything else is optional: a family
    filling this in on a phone should not be refused for not knowing a postcode.
    """
    # The child.
    student_full_name: str = Field(..., min_length=2, max_length=150)
    date_of_birth: date
    gender: Gender | None = None
    blood_group: str | None = Field(None, max_length=8)
    nationality: str | None = Field(None, max_length=100)
    previous_school: str | None = Field(None, max_length=200)
    previous_class: str | None = Field(None, max_length=100)

    # What they are applying for. `class_id` comes from the options endpoint; `class_applied`
    # is the free-text fallback for a school that has not set its classes up yet.
    class_id: int | None = None
    class_applied: str | None = Field(None, max_length=100)
    academic_year_id: int | None = Field(
        None, description="A year the options endpoint listed as open. Omit for the default."
    )
    syllabus: str | None = Field(None, max_length=100)
    medium: str | None = Field(None, max_length=50)

    parents: list[AdmissionParentIn] = Field(..., min_length=1, max_length=3)

    address_line1: str | None = Field(None, max_length=200)
    address_line2: str | None = Field(None, max_length=200)
    city: str | None = Field(None, max_length=100)
    state: str | None = Field(None, max_length=100)
    postal_code: str | None = Field(None, max_length=20)
    country: str | None = Field(None, max_length=100)

    sibling_name: str | None = Field(None, max_length=150, description="A sibling already here")
    transport_required: bool = False
    medical_notes: str | None = Field(None, max_length=2000)
    message: str | None = Field(None, max_length=2000)
    how_heard: str | None = Field(None, max_length=100)

    consent: bool = Field(..., description="The declaration that the details are accurate.")
    # A honeypot. Hidden on the real form, so anything that fills it in is a bot; the
    # request is then acknowledged and dropped rather than refused, which gives a scraper
    # nothing to learn from.
    website: str | None = Field(None, max_length=200)
    source: str | None = Field("website", max_length=50)

    @field_validator(
        "student_full_name", "blood_group", "nationality", "previous_school", "previous_class",
        "class_applied", "syllabus", "medium", "address_line1", "address_line2", "city",
        "state", "postal_code", "country", "sibling_name", "medical_notes", "message",
        "how_heard", "source", mode="before",
    )
    @classmethod
    def _strip(cls, value):
        return _blank_to_none(value)

    @field_validator("date_of_birth")
    @classmethod
    def _plausible_birth_date(cls, value: date):
        today = date.today()
        if value >= today:
            raise ValueError("The date of birth must be in the past")
        if value.year < today.year - 40:
            raise ValueError("Check the date of birth")
        return value

    @model_validator(mode="after")
    def _whole_form(self):
        if not self.consent:
            raise ValueError("Please confirm that the details given are accurate")
        if self.class_id is None and not self.class_applied:
            raise ValueError("Tell us which class the student is applying for")

        primaries = [p for p in self.parents if p.is_primary]
        if len(primaries) > 1:
            raise ValueError("Only one parent or guardian can be the primary contact")
        primary = primaries[0] if primaries else self.parents[0]
        if not primary.phone:
            raise ValueError("A phone number is needed for the primary contact")
        if not primary.email:
            raise ValueError("An email address is needed for the primary contact")
        return self


class AdmissionRequestSubmitted(BaseModel):
    """What the website shows after a successful submit."""
    id: int
    reference: str
    status: AdmissionRequestStatus
    student_full_name: str
    class_name: str | None = None
    academic_year_name: str | None = None
    submitted_at: datetime | None = None
    acknowledgement_sent: bool = False
    detail: str


class AdmissionOptionYear(BaseModel):
    id: int
    name: str
    start_date: date | None = None
    end_date: date | None = None
    is_current: bool = False


class AdmissionOptionClass(BaseModel):
    id: int
    name: str
    code: str | None = None


class AdmissionOptionsOut(BaseModel):
    """
    What the public form needs before it can render: whether applications are being taken,
    which session they land in and the classes on offer. Public, so it says nothing about
    who is in those classes.
    """
    accepting: bool
    closed_reason: str | None = None
    academic_year: AdmissionOptionYear | None = None
    academic_years: list[AdmissionOptionYear] = Field(default_factory=list)
    classes: list[AdmissionOptionClass] = Field(default_factory=list)
    relations: list[str] = Field(default_factory=list)


class AdmissionRequestNoteOut(BaseModel):
    author_id: int | None = None
    author_name: str | None = None
    body: str
    at: datetime | None = None


class AdmissionRequestHistoryOut(BaseModel):
    status: str
    at: datetime | None = None
    by: int | None = None
    by_name: str | None = None
    note: str | None = None


class AdmissionRequestOut(BaseModel):
    id: int
    reference: str
    program: str
    status: AdmissionRequestStatus

    student_full_name: str
    date_of_birth: date | None = None
    gender: str | None = None
    blood_group: str | None = None
    nationality: str | None = None
    previous_school: str | None = None
    previous_class: str | None = None

    class_id: int | None = None
    class_name: str | None = None
    academic_year_id: int | None = None
    academic_year_name: str | None = None
    syllabus: str | None = None
    medium: str | None = None

    parents: list[AdmissionParentOut] = Field(default_factory=list)
    contact_name: str | None = None
    contact_phone: str | None = None
    contact_email: str | None = None

    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None

    sibling_name: str | None = None
    transport_required: bool = False
    medical_notes: str | None = None
    message: str | None = None
    how_heard: str | None = None
    source: str | None = None

    submitted_at: datetime | None = None
    updated_at: datetime | None = None
    reviewed_by: int | None = None
    reviewed_by_name: str | None = None
    reviewed_at: datetime | None = None
    decision_note: str | None = None

    admitted_student_id: int | None = None
    admitted_student_email: str | None = None
    admitted_parent_id: int | None = None
    admitted_class_id: int | None = None
    admitted_class_name: str | None = None
    admitted_year_id: int | None = None
    admitted_year_name: str | None = None
    enrollment_id: int | None = None
    admission_number: str | None = None

    internal_notes: list[AdmissionRequestNoteOut] = Field(default_factory=list)
    history: list[AdmissionRequestHistoryOut] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class AdmissionRequestSummary(BaseModel):
    total: int = 0
    open: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)


class AdmissionRequestStatusUpdate(BaseModel):
    """
    Move a request between the non-terminal states, or decline it.

    ADMITTED is not accepted here: admitting creates accounts and has its own endpoint with
    its own options. An admitted request cannot be moved at all.
    """
    status: AdmissionRequestStatus
    note: str | None = Field(None, max_length=2000, description="Shown to the family if notified.")
    notify_applicant: bool = Field(
        False, description="Email the primary contact. Only sent for WAITLISTED and REJECTED."
    )

    @field_validator("status")
    @classmethod
    def _not_admitted(cls, value):
        if value == AdmissionRequestStatus.ADMITTED:
            raise ValueError("Use the admit endpoint to admit a request")
        return value

    @field_validator("note", mode="before")
    @classmethod
    def _strip(cls, value):
        return _blank_to_none(value)


class AdmissionRequestNoteCreate(BaseModel):
    body: str = Field(..., min_length=1, max_length=2000)

    @field_validator("body", mode="before")
    @classmethod
    def _strip(cls, value):
        return value.strip() if isinstance(value, str) else value


class AdmissionRequestAdmit(BaseModel):
    """
    Turn a request into a student.

    Every field is optional because the request already carries the answers; these override
    them. `class_id` defaults to the class applied for, `academic_year_id` to the session
    applied for (then the current year), and the admission number is issued by the school's
    identifier setting when left blank.

    `create_parent_account` makes a PARENT login for the primary contact (or links the
    student to an existing parent with that email) so the family can see fees and progress
    from day one. `send_credentials` issues passwords and emails them to the primary contact.
    """
    class_id: int | None = None
    academic_year_id: int | None = None
    admission_category_id: int | None = None
    admission_number: str | None = Field(None, max_length=50)
    roll_number: str | None = Field(None, max_length=50)
    student_email: str | None = Field(
        None, max_length=320, description="Login address; omit to derive one from the name."
    )
    create_parent_account: bool = True
    parent_email: str | None = Field(
        None, max_length=320, description="Defaults to the primary contact's email."
    )
    send_credentials: bool = False
    notify_applicant: bool = True
    note: str | None = Field(None, max_length=2000, description="Shown to the family if notified.")

    @field_validator("admission_number", "roll_number", "note", mode="before")
    @classmethod
    def _strip(cls, value):
        return _blank_to_none(value)

    @field_validator("student_email", "parent_email", mode="before")
    @classmethod
    def _lower_email(cls, value):
        cleaned = _blank_to_none(value)
        return cleaned.lower() if isinstance(cleaned, str) else cleaned


class AdmissionRequestAdmitResult(BaseModel):
    request: AdmissionRequestOut
    student: UserOut
    parent: UserOut | None = None
    parent_created: bool = False
    enrollment_id: int | None = None
    # Passwords issued during the admit, returned once so the office can relay them if the
    # email did not go. Never stored.
    credentials: list[CredentialsIssued] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    applicant_notified: bool = False
    detail: str
