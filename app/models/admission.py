"""
Admission bookkeeping: the session year a student was taken in, and the category they were
taken in under.

Both exist because the rest of the system keeps asking "which intake?" and, until now, had
nowhere to read the answer from. A class tells you where a student sits this term; it does
not tell you that they joined in 2024-25 as a transfer case on a staff concession, and that
last fact is what the fee rules, the discount rules and every year-on-year report need.

Kept apart from `ClassRoom` deliberately. A class is where teaching happens and it is rebuilt
every year; an academic year is the frame those classes hang in, and it outlives all of them.
"""

from dataclasses import dataclass, field
from datetime import date, datetime

from app.core.enums import AcademicYearStatus


@dataclass(slots=True)
class AcademicYear:
    """
    One session of the school - "2026-27", April to March - and the window it takes
    admissions in.

    `status` is stored rather than worked out from the dates; see `AcademicYearStatus` for
    why the calendar's answer and the school's answer are allowed to differ.

    Exactly one year should be `is_current` at a time, and the service enforces that by
    clearing the flag elsewhere when it is set here. Without that guarantee every "this
    year's students" query has to pick a winner arbitrarily, and different screens pick
    differently.
    """
    name: str                       # "2025-26"
    start_date: date
    end_date: date

    code: str | None = None         # "AY2526", for identifiers and exports.
    status: AcademicYearStatus = AcademicYearStatus.UPCOMING
    is_current: bool = False

    # Whether new students may be admitted into this year right now. Separate from `status`
    # because admissions for the next year routinely open while it is still UPCOMING, and
    # close while it is still ACTIVE once the class is full.
    admissions_open: bool = True

    # Which products this year applies to. The tuition programme runs on its own calendar in
    # some schools and shares the school's in others, so this is a list rather than assumed.
    programs: list[str] = field(default_factory=list)

    # The two halves the year is billed in: [{"key", "name", "start_date", "end_date"}].
    # Derived from the dates when the admin gives none - Term 1 from the start of the year
    # to the end of October, Term 2 from November to the end - and editable when a school
    # cuts it differently. Stored rather than always derived so an invoice raised in a term
    # can be read back against the dates that term actually had.
    terms: list[dict] = field(default_factory=list)

    notes: str | None = None
    created_by: int | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class AdmissionCategory:
    """
    The basis on which a student was admitted - "Regular", "Transfer", "Staff Ward", "RTE".

    This is the hook the fee module hangs concessions on. Modelled as its own record rather
    than a string on the profile because a category has to carry rules with it: a staff ward
    pays a different fee, and a management-quota seat may be exempt from the admission charge
    entirely. A free-text field on the student cannot be priced.

    `default_discount_percent` is the concession that comes with the category itself, applied
    before any sibling or multi-registration discount is worked out. Null means the category
    carries no concession of its own and exists purely as a label for reporting.
    """
    name: str
    code: str

    description: str | None = None
    # Scope to one year when a category is a one-off ("2025-26 Foundation Scholarship");
    # null makes it a standing category that every year inherits.
    academic_year_id: int | None = None
    programs: list[str] = field(default_factory=list)

    default_discount_percent: float | None = None
    # Set when the category waives the one-off joining charge rather than the recurring fee.
    waives_admission_charge: bool = False

    is_active: bool = True
    sort_order: int = 0

    created_by: int | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    id: int | None = None
