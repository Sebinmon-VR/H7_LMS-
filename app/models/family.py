"""
Families: sibling groups, and the parent logins that reach them.

Two separate ideas, kept as two collections on purpose.

A **sibling group** is a billing household. It exists so the fee module can answer "how many
children does this family have here?" - the question every sibling concession is built on -
and it has to work for a family that has never created a parent login, which is most of them
on day one. A group is therefore not derived from parent accounts; it is recorded by the
office when the second child is admitted.

A **parent link** is an access grant: this login may see that student. It is many-to-many
because a child usually has two parents and a parent usually has more than one child, and
because the pair a school actually deals with - one father with children in two different
sibling groups after a remarriage - has no sane representation as a field on either side.

Deriving one from the other was the obvious simplification and it is wrong in both
directions: two children can share a father without being billed as one household (different
mothers, different addresses, separate accounts), and a household can be billed together with
no parent login in existence at all.
"""

from dataclasses import dataclass, field
from datetime import datetime

from app.core.enums import GuardianRelation


@dataclass(slots=True)
class SiblingGroup:
    """
    One household, for fees and discounts.

    A student belongs to at most one group - the service enforces it - because the sibling
    concession asks "which child is this, first or second?" and a student sitting in two
    households has two answers and would be discounted twice.

    `student_ids` is stored on the group rather than a `sibling_group_id` on each student so
    that ordering is explicit. Sibling discounts are almost always "full fee for the eldest,
    concession for the rest", and that rule needs a defined first element, not a set. The
    list is held in admission order, oldest first.
    """
    family_name: str
    student_ids: list[int] = field(default_factory=list)

    # The person the office calls. Optional: a group can exist before anybody has a login.
    primary_contact_user_id: int | None = None
    primary_contact_name: str | None = None
    primary_contact_phone: str | None = None
    primary_contact_email: str | None = None

    # Shared household details, so an address change is one edit rather than one per child.
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str | None = None

    notes: str | None = None
    is_active: bool = True

    created_by: int | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class ParentLink:
    """
    One parent login's access to one student.

    The permission flags are per link rather than per parent account. A father who pays the
    bills and a grandmother who does the school run need different things, and the honest
    answer to "may this person see the invoices?" differs per child in exactly the separated
    households where getting it wrong matters most.

    `is_primary` marks the link that receives the scheduled reports and the fee notices, so
    a family with three linked adults gets one email rather than three.
    """
    parent_id: int
    student_id: int

    relation: GuardianRelation = GuardianRelation.GUARDIAN
    is_primary: bool = False

    # What this link may reach. Defaults are the reason a school creates the account at all:
    # academic progress and attendance. Fees are opt-in because they are the one thing a
    # non-paying guardian has no business reading.
    may_view_academics: bool = True
    may_view_attendance: bool = True
    may_view_fees: bool = False
    # Whether the scheduled weekly/monthly digest goes to this link.
    receives_reports: bool = True

    notes: str | None = None
    is_active: bool = True

    created_by: int | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    id: int | None = None
