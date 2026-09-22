"""
Request and response shapes for academic years and admission categories.
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.enums import AcademicTerm, AcademicYearStatus, Program


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
