"""
Request and response shapes for sibling groups and parent accounts.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import GuardianRelation, Program
from app.schemas.user import UserProfileFields


class SiblingGroupCreate(BaseModel):
    family_name: str = Field(..., min_length=1, max_length=150)
    # Accepted at creation because a group is almost always recorded at the moment the
    # second child is admitted, and both ids are on the screen already.
    student_ids: list[int] = Field(default_factory=list)

    primary_contact_user_id: int | None = None
    primary_contact_name: str | None = Field(None, max_length=150)
    primary_contact_phone: str | None = Field(None, max_length=32)
    primary_contact_email: str | None = Field(None, max_length=320)

    address_line1: str | None = Field(None, max_length=200)
    address_line2: str | None = Field(None, max_length=200)
    city: str | None = Field(None, max_length=100)
    state: str | None = Field(None, max_length=100)
    postal_code: str | None = Field(None, max_length=20)
    country: str | None = Field(None, max_length=100)

    notes: str | None = Field(None, max_length=2000)


class SiblingGroupUpdate(BaseModel):
    """
    Partial update; omitted fields are left unchanged.

    `student_ids` is absent on purpose. Membership changes go through the dedicated
    add/remove endpoints because each one has to check the student is not already in another
    household, and a wholesale list replacement would skip that check for every id in it.
    """
    family_name: str | None = Field(None, min_length=1, max_length=150)
    primary_contact_user_id: int | None = None
    primary_contact_name: str | None = Field(None, max_length=150)
    primary_contact_phone: str | None = Field(None, max_length=32)
    primary_contact_email: str | None = Field(None, max_length=320)

    address_line1: str | None = Field(None, max_length=200)
    address_line2: str | None = Field(None, max_length=200)
    city: str | None = Field(None, max_length=100)
    state: str | None = Field(None, max_length=100)
    postal_code: str | None = Field(None, max_length=20)
    country: str | None = Field(None, max_length=100)

    notes: str | None = Field(None, max_length=2000)
    is_active: bool | None = None


class SiblingMemberOut(BaseModel):
    """One child in a household, with just enough to render the family screen."""
    student_id: int
    full_name: str
    email: str | None = None
    admission_number: str | None = None
    class_id: int | None = None
    class_name: str | None = None
    is_active: bool = True
    # 1 for the eldest, counting up. What every sibling concession is priced from.
    birth_order: int


class SiblingGroupOut(BaseModel):
    id: int
    family_name: str
    student_ids: list[int] = Field(default_factory=list)
    members: list[SiblingMemberOut] = Field(default_factory=list)
    sibling_count: int = 0

    primary_contact_user_id: int | None = None
    primary_contact_name: str | None = None
    primary_contact_phone: str | None = None
    primary_contact_email: str | None = None

    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None

    notes: str | None = None
    is_active: bool = True
    # True when the household was built by the guardian-details sweep rather than typed in,
    # with the details it matched on. The office's cue to check the name it was given.
    auto_mapped: bool = False
    matched_by: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class SiblingMemberAdd(BaseModel):
    student_id: int


# ---------------------------------------------------------------------------------------
# Automatic mapping
# ---------------------------------------------------------------------------------------


class FamilyAutoMapRequest(BaseModel):
    """
    `POST /admin/families/auto-map`. Empty `student_ids` sweeps every ungrouped student.
    """
    student_ids: list[int] = Field(
        default_factory=list, description="Restrict to these students; empty means everyone."
    )
    dry_run: bool = Field(
        False, description="Report what would happen without creating or changing anything."
    )


class FamilyMatch(BaseModel):
    """Another student who looks like a sibling, and why."""
    student_id: int
    full_name: str | None = None
    admission_number: str | None = None
    guardian_name: str | None = None
    guardian_phone: str | None = None
    guardian_email: str | None = None
    # guardian_phone | guardian_email | parent_link are acted on; guardian_name, address
    # and postal_code are suggestions only.
    matched_by: list[str] = Field(default_factory=list)
    group_id: int | None = None
    group_name: str | None = None


class FamilyMatchesOut(BaseModel):
    """
    `GET /admin/families/students/{id}/matches`: what the sweep sees for one student.
    `strong` is what it would act on; `suggestions` is what it leaves to the office.
    """
    student: FamilyMatch
    strong: list[FamilyMatch] = Field(default_factory=list)
    suggestions: list[FamilyMatch] = Field(default_factory=list)


class FamilyAutoMapResult(BaseModel):
    student_id: int
    full_name: str | None = None
    admission_number: str | None = None
    # already_grouped | added | created | conflict | no_match | skipped
    action: str
    reason: str | None = None
    matched_by: list[str] = Field(default_factory=list)
    group_id: int | None = None
    group_name: str | None = None
    matches: list[FamilyMatch] = Field(default_factory=list)
    suggestions: list[FamilyMatch] = Field(default_factory=list)
    candidate_groups: list[dict] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")


class FamilyAutoMapReport(BaseModel):
    dry_run: bool
    considered: int
    created: list[FamilyAutoMapResult] = Field(default_factory=list)
    added: list[FamilyAutoMapResult] = Field(default_factory=list)
    conflicts: list[FamilyAutoMapResult] = Field(default_factory=list)
    unmatched: list[FamilyAutoMapResult] = Field(default_factory=list)
    skipped: list[FamilyAutoMapResult] = Field(default_factory=list)
    already_grouped: int = 0
    summary: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------------------
# Parent accounts
# ---------------------------------------------------------------------------------------


class ParentCreate(UserProfileFields):
    """
    Creates a parent login and links it to children in one call.

    Inherits the shared profile fields so a parent carries the same contact and address
    details as anybody else - the front office needs a phone number for a father exactly as
    much as for a student.

    `role` is absent by design: this endpoint mints parents, and letting a request body name
    the role is how a create-parent form becomes an admin-creation exploit.
    """
    full_name: str = Field(..., min_length=1, max_length=150)
    email: str | None = Field(
        None, description="Omit to derive one from the name, as with any other account."
    )
    password: str | None = None
    programs: list[Program] = Field(
        default_factory=lambda: [Program.LMS],
        description="Products this parent may reach, matching their children's.",
    )
    # [{"student_id", "relation", "is_primary", "may_view_fees", ...}]
    links: list["ParentLinkCreate"] = Field(default_factory=list)


class ParentLinkCreate(BaseModel):
    student_id: int
    relation: GuardianRelation = GuardianRelation.GUARDIAN
    is_primary: bool = False
    may_view_academics: bool = True
    may_view_attendance: bool = True
    may_view_fees: bool = False
    receives_reports: bool = True
    notes: str | None = Field(None, max_length=1000)


class ParentLinkUpdate(BaseModel):
    """Partial update; omitted fields are left unchanged."""
    relation: GuardianRelation | None = None
    is_primary: bool | None = None
    may_view_academics: bool | None = None
    may_view_attendance: bool | None = None
    may_view_fees: bool | None = None
    receives_reports: bool | None = None
    notes: str | None = Field(None, max_length=1000)
    is_active: bool | None = None


class ParentLinkOut(BaseModel):
    id: int
    parent_id: int
    parent_name: str | None = None
    parent_email: str | None = None
    student_id: int
    student_name: str | None = None
    student_admission_number: str | None = None
    class_id: int | None = None
    class_name: str | None = None

    relation: GuardianRelation
    is_primary: bool = False
    may_view_academics: bool = True
    may_view_attendance: bool = True
    may_view_fees: bool = False
    receives_reports: bool = True
    notes: str | None = None
    is_active: bool = True
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class ChildSummary(BaseModel):
    """
    One child as their parent sees them on the switcher.

    Carries the permission flags from the link so the frontend can hide the fee tab rather
    than render it and have the request rejected - the same answer, arrived at before the
    parent has clicked something they were never allowed to open.
    """
    student_id: int
    full_name: str
    email: str | None = None
    photo_url: str | None = None
    admission_number: str | None = None
    roll_number: str | None = None
    class_id: int | None = None
    class_name: str | None = None
    relation: GuardianRelation
    is_primary: bool = False
    may_view_academics: bool = True
    may_view_attendance: bool = True
    may_view_fees: bool = False
    programs: list[str] = Field(default_factory=list)


ParentCreate.model_rebuild()
