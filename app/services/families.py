"""
Sibling groups and parent accounts.

Two rules carry this module, and both exist to stop money and privacy going wrong.

**One household per student.** `add_member` refuses a student who already belongs to a group.
Every sibling concession is priced from "which child is this, first or second?", and a
student sitting in two households answers that twice and is discounted twice. The check is
here rather than in the schema because it needs the whole collection to answer.

**Households build themselves where the data allows.** Two students recording the same
guardian phone or email, or reached by the same parent login, are siblings, and `auto_map`
groups them without anybody clicking - on admission, on a guardian edit, on a parent link,
or in one sweep over the whole roll. Anything weaker (a shared guardian *name*) is reported
as a suggestion for the office, which is what the manual endpoints are for.

**A parent sees only what their link grants.** `assert_may_view` is the single gate, and it
takes the aspect being read - academics, attendance, fees - because the honest answer differs
per aspect and per child. A father who pays the bills and a grandmother who does the school
run are both legitimately linked to the same student and must not see the same screens.

Parent access is resolved per request rather than baked into the token. A link revoked this
morning has to stop working this morning, and a claim minted at login would keep working
until it expired.
"""

import logging
from datetime import datetime

from fastapi import HTTPException

from app.core.enums import GuardianRelation, Program, UserRole
from app.core.firebase import (
    firestore_class_teacher_mappings, firestore_classes, firestore_parent_links,
    firestore_sibling_groups, firestore_student_enrollments, firestore_users,
    require_document,
)

logger = logging.getLogger("families")

# The aspects a link can grant, mapped to the flag that grants each. Named here so a caller
# asking for an aspect that does not exist fails loudly instead of silently passing the
# check - `record.get("may_view_typo")` is None, and None is falsy, which would have denied
# rather than allowed, but just as wrongly and far more confusingly.
VIEW_FLAGS = {
    "academics": "may_view_academics",
    "attendance": "may_view_attendance",
    "fees": "may_view_fees",
}


def _now() -> str:
    return datetime.utcnow().isoformat()


# ---------------------------------------------------------------------------------------
# Sibling groups
# ---------------------------------------------------------------------------------------

def require_group(group_id) -> dict:
    return require_document(firestore_sibling_groups, group_id, "Sibling group")


def _member_ids(group: dict) -> list[int]:
    """The membership list, cleaned. Tolerates ids stored as strings by an older write."""
    out = []
    for value in group.get("student_ids") or []:
        try:
            member_id = int(value)
        except (TypeError, ValueError):
            continue
        if member_id not in out:
            out.append(member_id)
    return out


def group_for_student(student_id) -> dict | None:
    """
    The household a student belongs to, or None.

    Scans rather than queries: Firestore's `array_contains` would serve this, but the
    collection is small, it is cached, and the scan keeps the "at most one group" invariant
    checkable in the same pass - a query returning two rows would need this loop anyway to
    decide which one is real.
    """
    wanted = int(student_id)
    for group in firestore_sibling_groups.list_all():
        if wanted in _member_ids(group):
            return group
    return None


def birth_order(group: dict, student_id) -> int:
    """
    1 for the eldest child in the household, counting up.

    Derived from the stored order of `student_ids`, which `add_member` keeps in admission
    order. Returns 1 for a student not in the group, so a caller that lost track of the
    membership prices a full fee rather than a concession - the error that costs the school
    nothing and the family nothing they were not already expecting.
    """
    members = _member_ids(group)
    try:
        return members.index(int(student_id)) + 1
    except ValueError:
        return 1


def sibling_count(student_id) -> int:
    """
    How many children this family has enrolled, including this one.

    1 when the student has no group, which is the correct answer rather than a missing one:
    an only child and an unrecorded family are priced identically, and a concession that
    needed the record to exist would quietly overcharge every family the office had not got
    round to linking.
    """
    group = group_for_student(student_id)
    if not group:
        return 1
    active = [
        member for member in _member_ids(group)
        if (firestore_users.get_document(str(member)) or {}).get("is_active", False)
    ]
    return max(len(active), 1)


def _assert_is_student(student_id) -> dict:
    student = require_document(firestore_users, student_id, "Student")
    if student.get("role") != UserRole.STUDENT.value:
        raise HTTPException(
            status_code=400,
            detail=f"User {student_id} is a {student.get('role')}, not a student. "
                   "Only students belong to a sibling group.",
        )
    return student


def create_group(payload, actor_id: int | None = None) -> dict:
    group_id = firestore_sibling_groups.get_next_numeric_id()

    # Validated before the group exists, so a bad id fails without leaving an empty
    # household behind for somebody to clean up.
    members: list[int] = []
    for student_id in payload.student_ids or []:
        _assert_is_student(student_id)
        existing = group_for_student(student_id)
        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"Student {student_id} already belongs to the "
                       f"'{existing.get('family_name')}' household (group {existing['id']}). "
                       "Remove them from it first.",
            )
        if int(student_id) not in members:
            members.append(int(student_id))

    document = {
        "family_name": payload.family_name.strip(),
        "student_ids": members,
        "primary_contact_user_id": payload.primary_contact_user_id,
        "primary_contact_name": payload.primary_contact_name,
        "primary_contact_phone": payload.primary_contact_phone,
        "primary_contact_email": payload.primary_contact_email,
        "address_line1": payload.address_line1,
        "address_line2": payload.address_line2,
        "city": payload.city,
        "state": payload.state,
        "postal_code": payload.postal_code,
        "country": payload.country,
        "notes": payload.notes,
        "is_active": True,
        "created_by": actor_id,
        "created_at": _now(),
    }
    firestore_sibling_groups.add_document(str(group_id), document)
    document["id"] = group_id
    logger.info("Created sibling group %s (%s) with %s member(s).",
                group_id, document["family_name"], len(members))
    return document


def update_group(group: dict, payload, actor_id: int | None = None) -> dict:
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    updates["updated_at"] = _now()
    updates["updated_by"] = actor_id
    firestore_sibling_groups.add_document(str(group["id"]), updates)
    return {**group, **updates}


def add_member(group: dict, student_id, actor_id: int | None = None) -> dict:
    """Adds a child to the household, enforcing the one-group-per-student rule."""
    _assert_is_student(student_id)

    existing = group_for_student(student_id)
    if existing and str(existing["id"]) != str(group["id"]):
        raise HTTPException(
            status_code=400,
            detail=f"Student {student_id} already belongs to the "
                   f"'{existing.get('family_name')}' household (group {existing['id']}).",
        )

    members = _member_ids(group)
    if int(student_id) in members:
        return group

    # Appended rather than inserted by age: the office adds children in admission order,
    # which is the order the concession is meant to run in, and re-sorting by a date of
    # birth that is frequently blank would shuffle who pays full fee.
    members.append(int(student_id))
    updates = {"student_ids": members, "updated_at": _now(), "updated_by": actor_id}
    firestore_sibling_groups.add_document(str(group["id"]), updates)
    return {**group, **updates}


def remove_member(group: dict, student_id, actor_id: int | None = None) -> dict:
    members = [m for m in _member_ids(group) if m != int(student_id)]
    if len(members) == len(_member_ids(group)):
        raise HTTPException(
            status_code=404,
            detail=f"Student {student_id} is not a member of this household.",
        )
    updates = {"student_ids": members, "updated_at": _now(), "updated_by": actor_id}
    firestore_sibling_groups.add_document(str(group["id"]), updates)
    return {**group, **updates}


def delete_group(group: dict) -> None:
    """
    Deletes a household outright.

    Safe to cascade nothing: a group holds no records of its own, and the students it named
    are untouched by its removal. The only loss is the sibling concession, which is a pricing
    decision the admin is making deliberately by deleting the group.
    """
    firestore_sibling_groups.delete_document(str(group["id"]))
    logger.info("Deleted sibling group %s (%s).", group["id"], group.get("family_name"))


def _class_of(student_id) -> tuple[int | None, str | None]:
    """The class a student is enrolled in, as (id, name). Both None when unenrolled."""
    enrollments = firestore_student_enrollments.query_documents(
        "student_id", "==", int(student_id)
    )
    if not enrollments:
        return None, None
    class_id = enrollments[0].get("class_id")
    if class_id is None:
        return None, None
    class_room = firestore_classes.get_document(str(class_id))
    return int(class_id), (class_room or {}).get("name")


def present_group(group: dict, with_members: bool = True) -> dict:
    members = _member_ids(group)
    view = {
        "id": int(group["id"]),
        "family_name": group.get("family_name"),
        "student_ids": members,
        "sibling_count": len(members),
        "primary_contact_user_id": group.get("primary_contact_user_id"),
        "primary_contact_name": group.get("primary_contact_name"),
        "primary_contact_phone": group.get("primary_contact_phone"),
        "primary_contact_email": group.get("primary_contact_email"),
        "address_line1": group.get("address_line1"),
        "address_line2": group.get("address_line2"),
        "city": group.get("city"),
        "state": group.get("state"),
        "postal_code": group.get("postal_code"),
        "country": group.get("country"),
        "notes": group.get("notes"),
        "is_active": bool(group.get("is_active", True)),
        "auto_mapped": bool(group.get("auto_mapped")),
        "matched_by": list(group.get("matched_by") or []),
        "created_at": group.get("created_at"),
        "updated_at": group.get("updated_at"),
        "members": [],
    }

    if with_members:
        for position, student_id in enumerate(members, start=1):
            student = firestore_users.get_document(str(student_id))
            if not student:
                continue
            class_id, class_name = _class_of(student_id)
            view["members"].append({
                "student_id": int(student_id),
                "full_name": student.get("full_name"),
                "email": student.get("email"),
                "admission_number": student.get("admission_number"),
                "class_id": class_id,
                "class_name": class_name,
                "is_active": bool(student.get("is_active", True)),
                "birth_order": position,
            })

    return view


# ---------------------------------------------------------------------------------------
# Parent accounts and links
# ---------------------------------------------------------------------------------------

def require_link(link_id) -> dict:
    return require_document(firestore_parent_links, link_id, "Parent link")


def links_for_parent(parent_id, active_only: bool = True) -> list[dict]:
    links = firestore_parent_links.query_documents("parent_id", "==", int(parent_id))
    if active_only:
        links = [link for link in links if link.get("is_active", True)]
    return links


def links_for_student(student_id, active_only: bool = True) -> list[dict]:
    links = firestore_parent_links.query_documents("student_id", "==", int(student_id))
    if active_only:
        links = [link for link in links if link.get("is_active", True)]
    return links


def child_ids(parent_id) -> set[int]:
    """Every student this parent may reach at all, whatever the per-aspect flags say."""
    return {int(link["student_id"]) for link in links_for_parent(parent_id)}


def link_between(parent_id, student_id) -> dict | None:
    for link in links_for_parent(parent_id):
        if int(link.get("student_id", -1)) == int(student_id):
            return link
    return None


def create_link(parent_id, payload, actor_id: int | None = None) -> dict:
    """
    Grants one parent access to one student.

    A repeat of an existing pair updates it rather than adding a second row. Two live links
    between the same two people would give two different answers to "may they see the fees?",
    and which one won would depend on query order.
    """
    parent = require_document(firestore_users, parent_id, "Parent")
    if parent.get("role") != UserRole.PARENT.value:
        raise HTTPException(
            status_code=400,
            detail=f"User {parent_id} is a {parent.get('role')}, not a parent.",
        )
    _assert_is_student(payload.student_id)

    existing = link_between(parent_id, payload.student_id)
    fields = {
        "relation": getattr(payload.relation, "value", payload.relation),
        "is_primary": bool(payload.is_primary),
        "may_view_academics": bool(payload.may_view_academics),
        "may_view_attendance": bool(payload.may_view_attendance),
        "may_view_fees": bool(payload.may_view_fees),
        "receives_reports": bool(payload.receives_reports),
        "notes": payload.notes,
        "is_active": True,
    }

    if existing:
        fields["updated_at"] = _now()
        fields["updated_by"] = actor_id
        firestore_parent_links.add_document(str(existing["id"]), fields)
        result = {**existing, **fields}
    else:
        link_id = firestore_parent_links.get_next_numeric_id()
        document = {
            **fields,
            "parent_id": int(parent_id),
            "student_id": int(payload.student_id),
            "created_by": actor_id,
            "created_at": _now(),
        }
        firestore_parent_links.add_document(str(link_id), document)
        document["id"] = link_id
        result = document

    if fields["is_primary"]:
        _make_primary(payload.student_id, result["id"])

    logger.info("Linked parent %s to student %s.", parent_id, payload.student_id)
    # Two children reached by one login are one household. Placed here rather than by the
    # caller so every route that links a parent gets the same behaviour.
    auto_place(payload.student_id, actor_id)
    return result


def _make_primary(student_id, link_id) -> None:
    """
    Clears `is_primary` on the student's other links.

    The primary link is who the scheduled reports and fee notices go to. Two primaries means
    a family gets everything twice, which is the kind of thing that turns a weekly digest
    into a complaint.
    """
    for other in links_for_student(student_id, active_only=False):
        if str(other["id"]) != str(link_id) and other.get("is_primary"):
            firestore_parent_links.add_document(str(other["id"]), {"is_primary": False})


def update_link(link: dict, payload, actor_id: int | None = None) -> dict:
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    if "relation" in updates:
        updates["relation"] = getattr(updates["relation"], "value", updates["relation"])

    updates["updated_at"] = _now()
    updates["updated_by"] = actor_id
    firestore_parent_links.add_document(str(link["id"]), updates)

    if updates.get("is_primary"):
        _make_primary(link["student_id"], link["id"])

    return {**link, **updates}


def delete_link(link: dict) -> None:
    firestore_parent_links.delete_document(str(link["id"]))
    logger.info("Unlinked parent %s from student %s.",
                link.get("parent_id"), link.get("student_id"))


def assert_may_view(parent, student_id, aspect: str = "academics") -> dict:
    """
    The single gate on every parent-facing read.

    Admins pass unconditionally - they can already see the student through the admin module,
    and making them create a parent link to use a parent endpoint would be ceremony, not
    security.

    Raises 403 rather than 404 for a student the parent is linked to but not permitted to see
    this aspect of, and 403 for one they are not linked to at all. Deliberately the same
    status and a similar message: distinguishing them would let a parent enumerate which
    student ids exist by reading the error text.
    """
    if getattr(parent, "role", None) == UserRole.ADMIN:
        return {}

    flag = VIEW_FLAGS.get(aspect)
    if flag is None:
        raise ValueError(f"Unknown aspect '{aspect}'; expected one of {sorted(VIEW_FLAGS)}")

    link = link_between(getattr(parent, "id", parent), student_id)
    if not link or not link.get(flag, False):
        raise HTTPException(
            status_code=403,
            detail=f"You do not have access to this student's {aspect}.",
        )
    return link


def children_of(parent_id) -> list[dict]:
    """
    The switcher: every child this parent may open, with the permissions attached.

    Sorted with the primary link first and then by name, so the parent of three sees a stable
    order rather than whatever Firestore returned that second.
    """
    summaries = []
    for link in links_for_parent(parent_id):
        student = firestore_users.get_document(str(link["student_id"]))
        if not student or not student.get("is_active", True):
            continue
        class_id, class_name = _class_of(link["student_id"])
        summaries.append({
            "student_id": int(link["student_id"]),
            "full_name": student.get("full_name"),
            "email": student.get("email"),
            "photo_url": student.get("photo_url"),
            "admission_number": student.get("admission_number"),
            "roll_number": student.get("roll_number"),
            "class_id": class_id,
            "class_name": class_name,
            "relation": link.get("relation") or GuardianRelation.GUARDIAN.value,
            "is_primary": bool(link.get("is_primary")),
            "may_view_academics": bool(link.get("may_view_academics", True)),
            "may_view_attendance": bool(link.get("may_view_attendance", True)),
            "may_view_fees": bool(link.get("may_view_fees", False)),
            "programs": student.get("programs") or [Program.LMS.value],
        })

    return sorted(summaries, key=lambda c: (not c["is_primary"], str(c["full_name"] or "")))


def present_link(link: dict) -> dict:
    parent = firestore_users.get_document(str(link.get("parent_id"))) or {}
    student = firestore_users.get_document(str(link.get("student_id"))) or {}
    class_id, class_name = _class_of(link["student_id"])

    return {
        "id": int(link["id"]),
        "parent_id": int(link["parent_id"]),
        "parent_name": parent.get("full_name"),
        "parent_email": parent.get("email"),
        "student_id": int(link["student_id"]),
        "student_name": student.get("full_name"),
        "student_admission_number": student.get("admission_number"),
        "class_id": class_id,
        "class_name": class_name,
        "relation": link.get("relation") or GuardianRelation.GUARDIAN.value,
        "is_primary": bool(link.get("is_primary")),
        "may_view_academics": bool(link.get("may_view_academics", True)),
        "may_view_attendance": bool(link.get("may_view_attendance", True)),
        "may_view_fees": bool(link.get("may_view_fees", False)),
        "receives_reports": bool(link.get("receives_reports", True)),
        "notes": link.get("notes"),
        "is_active": bool(link.get("is_active", True)),
        "created_at": link.get("created_at"),
    }


def report_recipients(student_id) -> list[dict]:
    """
    Who the scheduled digest for a student should go to.

    Used by the weekly/monthly parent reports. Falls back to the guardian email recorded on
    the student's own profile when no parent account exists, because most families will never
    create a login and a report nobody receives is not a report.
    """
    recipients = []
    for link in links_for_student(student_id):
        if not link.get("receives_reports", True):
            continue
        parent = firestore_users.get_document(str(link["parent_id"]))
        if parent and parent.get("is_active", True) and parent.get("email"):
            recipients.append({
                "user_id": int(parent["id"]),
                "name": parent.get("full_name"),
                "email": parent["email"],
                "is_primary": bool(link.get("is_primary")),
            })

    if recipients:
        return sorted(recipients, key=lambda r: not r["is_primary"])

    student = firestore_users.get_document(str(student_id)) or {}
    if student.get("guardian_email"):
        return [{
            "user_id": None,
            "name": student.get("guardian_name") or "Parent/Guardian",
            "email": student["guardian_email"],
            "is_primary": True,
        }]
    return []


def teacher_ids_for_classes(class_ids) -> set[int]:
    """
    Every teacher answerable for a set of classes.

    Lives here rather than in `permissions` because it answers a different question: not
    "may this teacher act on the class?" but "who should be told about it?". The notice board
    is its first caller and the parent reports will be its second.
    """
    wanted = {int(c) for c in class_ids}
    teachers = set()
    for mapping in firestore_class_teacher_mappings.list_all():
        if int(mapping.get("class_id", -1)) in wanted:
            teachers.add(int(mapping["teacher_id"]))
    return teachers


# ---------------------------------------------------------------------------------------
# Automatic household mapping
#
# The office records a guardian on every student; two students with the same guardian are
# one household. That is enough to build most sibling groups without anybody clicking, so
# they are built - on admission, on a guardian edit, on a parent link - and the manual
# endpoints above remain for the cases the data cannot decide.
#
# What counts as "the same guardian" is deliberately narrow. A shared guardian **phone**, a
# shared guardian **email**, or a shared **parent login** are each strong enough to act on:
# they are contact details one family owns. A shared guardian *name* is not - "Mohammed" and
# "Priya" are half a school - so a name match is reported as a suggestion for the office to
# confirm, never acted on by itself.
#
# Planning is separated from writing. `plan_auto_map` decides everything against an
# in-memory snapshot and returns a list of actions; `apply_auto_map` performs them. That is
# what makes a dry run exactly the real run minus the writes, and what lets one sweep place
# three siblings into a group that did not exist when it started.
# ---------------------------------------------------------------------------------------

STRONG_BASES = ("guardian_phone", "guardian_email", "parent_link")


def _digits(value) -> str:
    """
    A phone number reduced to what identifies it.

    Digits only, and the last ten of them: "+91 98765 43210", "09876543210" and
    "9876543210" are one phone, and treating them as three is how a family gets three
    households.
    """
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _norm(value) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _surname(full_name) -> str:
    parts = _norm(full_name).split()
    return parts[-1] if len(parts) > 1 else ""


def _fingerprints(student: dict) -> dict:
    return {
        "guardian_phone": _digits(student.get("guardian_phone")),
        "guardian_email": _norm(student.get("guardian_email")),
        "guardian_name": _norm(student.get("guardian_name")),
        "address": _norm(student.get("address_line1")),
        "postal_code": _norm(student.get("postal_code")),
    }


def _matches(a: dict, b: dict, parents: dict[int, set[int]]) -> tuple[list[str], list[str]]:
    """
    Why two students look like siblings: the strong reasons, and the weak ones.

    Returns two lists of basis names. Strong reasons are acted on; weak ones are only
    reported. A pair with no reason of either kind returns two empty lists.
    """
    fa, fb = _fingerprints(a), _fingerprints(b)
    strong, weak = [], []

    if fa["guardian_phone"] and len(fa["guardian_phone"]) >= 7 \
            and fa["guardian_phone"] == fb["guardian_phone"]:
        strong.append("guardian_phone")
    if fa["guardian_email"] and fa["guardian_email"] == fb["guardian_email"]:
        strong.append("guardian_email")
    if parents.get(int(a["id"]), set()) & parents.get(int(b["id"]), set()):
        strong.append("parent_link")

    if not strong and fa["guardian_name"] and len(fa["guardian_name"]) >= 3 \
            and fa["guardian_name"] == fb["guardian_name"]:
        # A name is a suggestion. With an address behind it, a stronger one - but still a
        # suggestion, because two families in one building is not one family.
        weak.append("guardian_name")
        if fa["address"] and fa["address"] == fb["address"]:
            weak.append("address")
        elif fa["postal_code"] and fa["postal_code"] == fb["postal_code"]:
            weak.append("postal_code")

    return strong, weak


def _snapshot() -> dict:
    """Everything the planner needs, read once."""
    students = [
        u for u in firestore_users.list_all()
        if u.get("role") == UserRole.STUDENT.value and u.get("is_active", True)
    ]
    groups = [g for g in firestore_sibling_groups.list_all() if g.get("is_active", True)]
    parents: dict[int, set[int]] = {}
    for link in firestore_parent_links.list_all():
        if not link.get("is_active", True):
            continue
        try:
            parents.setdefault(int(link["student_id"]), set()).add(int(link["parent_id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return {"students": students, "groups": groups, "parents": parents}


def _group_of(snapshot: dict, student_id: int) -> dict | None:
    for group in snapshot["groups"]:
        if student_id in _member_ids(group):
            return group
    return None


def _admission_order(student: dict) -> str:
    """Eldest first, as the office admitted them: admission date, then account creation."""
    return str(student.get("admission_date") or "9999") + "|" + str(student.get("created_at") or "")


def _family_name(members: list[dict]) -> str:
    """
    "<Surname> family" when the children share one, else the guardian's name, else the
    first child's - the label the office would have typed, derived so it need not be.
    """
    surnames = {_surname(m.get("full_name")) for m in members} - {""}
    if len(surnames) == 1:
        return f"{next(iter(surnames)).title()} family"
    for member in members:
        if member.get("guardian_name"):
            return f"{str(member['guardian_name']).strip()} family"
    return f"{members[0].get('full_name') or 'Unnamed'} family"


def _summary(student: dict) -> dict:
    return {
        "student_id": int(student["id"]),
        "full_name": student.get("full_name"),
        "admission_number": student.get("admission_number"),
        "guardian_name": student.get("guardian_name"),
        "guardian_phone": student.get("guardian_phone"),
        "guardian_email": student.get("guardian_email"),
    }


def find_matches(student: dict, snapshot: dict | None = None) -> dict:
    """
    Every other student who looks like this one's sibling, with the reason.

    `strong` are the matches the sweep acts on; `suggestions` are the name-only ones it
    leaves to the office. Both carry the household the other student is already in, if any,
    so the admin screen can say "add to the Sharma household" rather than "create one".
    """
    snapshot = snapshot or _snapshot()
    strong, weak = [], []
    for other in snapshot["students"]:
        if int(other["id"]) == int(student["id"]):
            continue
        strong_bases, weak_bases = _matches(student, other, snapshot["parents"])
        if not strong_bases and not weak_bases:
            continue
        group = _group_of(snapshot, int(other["id"]))
        entry = {
            **_summary(other),
            "matched_by": strong_bases or weak_bases,
            "group_id": int(group["id"]) if group and group.get("id") is not None else None,
            "group_name": group.get("family_name") if group else None,
        }
        (strong if strong_bases else weak).append(entry)
    return {"student": _summary(student), "strong": strong, "suggestions": weak}


def plan_auto_map(snapshot: dict, targets: list[dict]) -> list[dict]:
    """
    Decides, without writing, what each target student's household should be.

    Works through the targets in admission order and updates the snapshot as it goes, so
    the second of three new siblings finds the group the first one just planned. Each
    result is one of:

        already_grouped - nothing to do
        added           - joins the one household its matches are in
        created         - a new household, from matches in none
        conflict        - matches sit in more than one household; left for the office
        no_match        - nothing strong enough to act on (suggestions listed, if any)
        skipped         - no guardian detail at all to match on
    """
    results = []
    for student in sorted(targets, key=_admission_order):
        student_id = int(student["id"])
        base = {"student_id": student_id, "full_name": student.get("full_name"),
                "admission_number": student.get("admission_number")}

        existing = _group_of(snapshot, student_id)
        if existing:
            results.append({**base, "action": "already_grouped",
                            "group_id": existing.get("id"),
                            "group_name": existing.get("family_name")})
            continue

        prints = _fingerprints(student)
        if not (prints["guardian_phone"] or prints["guardian_email"]
                or prints["guardian_name"] or snapshot["parents"].get(student_id)):
            results.append({**base, "action": "skipped",
                            "reason": "No guardian phone, email, name or parent login recorded."})
            continue

        found = find_matches(student, snapshot)
        strong = found["strong"]
        if not strong:
            results.append({
                **base, "action": "no_match",
                "reason": "No other student shares this guardian's phone, email or login."
                          + (" Name-only matches are listed as suggestions."
                             if found["suggestions"] else ""),
                "suggestions": found["suggestions"],
            })
            continue

        groups = {}
        for match in strong:
            group = _group_of(snapshot, match["student_id"])
            if group:
                groups[id(group)] = group
        bases = sorted({b for m in strong for b in m["matched_by"]})

        if len(groups) > 1:
            results.append({
                **base, "action": "conflict",
                "reason": "Matching students are already in different households.",
                "matches": strong,
                "candidate_groups": [
                    {"group_id": g.get("id"), "group_name": g.get("family_name")}
                    for g in groups.values()
                ],
            })
            continue

        if groups:
            group = next(iter(groups.values()))
            group["student_ids"] = _member_ids(group) + [student_id]
            results.append({
                **base, "action": "added", "matched_by": bases,
                "group_id": group.get("id"), "group_name": group.get("family_name"),
                "matches": strong,
            })
            continue

        members = sorted(
            [student] + [
                s for s in snapshot["students"]
                if int(s["id"]) in {m["student_id"] for m in strong}
            ],
            key=_admission_order,
        )
        contact = next((m for m in members if m.get("guardian_phone") or m.get("guardian_email")),
                       members[0])
        planned = {
            "id": None,
            "family_name": _family_name(members),
            "student_ids": [int(m["id"]) for m in members],
            "primary_contact_name": contact.get("guardian_name"),
            "primary_contact_phone": contact.get("guardian_phone"),
            "primary_contact_email": contact.get("guardian_email"),
            "address_line1": contact.get("address_line1"),
            "address_line2": contact.get("address_line2"),
            "city": contact.get("city"),
            "state": contact.get("state"),
            "postal_code": contact.get("postal_code"),
            "country": contact.get("country"),
            "is_active": True,
            "auto_mapped": True,
            "matched_by": bases,
        }
        snapshot["groups"].append(planned)
        results.append({
            **base, "action": "created", "matched_by": bases,
            "group_id": None, "group_name": planned["family_name"],
            "planned_group": planned,
            "matches": strong,
        })

    return results


def apply_auto_map(results: list[dict], actor_id: int | None = None) -> list[dict]:
    """
    Performs a plan. Each `created` result is written once and its id filled in; an `added`
    result whose group was planned in the same sweep resolves to the id that write produced.
    """
    from types import SimpleNamespace

    created_ids: dict[int, int] = {}   # id(planned dict) -> stored group id

    for result in results:
        if result.get("action") == "created":
            planned = result["planned_group"]
            payload = SimpleNamespace(
                family_name=planned["family_name"],
                student_ids=[],
                primary_contact_user_id=None,
                primary_contact_name=planned.get("primary_contact_name"),
                primary_contact_phone=planned.get("primary_contact_phone"),
                primary_contact_email=planned.get("primary_contact_email"),
                address_line1=planned.get("address_line1"),
                address_line2=planned.get("address_line2"),
                city=planned.get("city"),
                state=planned.get("state"),
                postal_code=planned.get("postal_code"),
                country=planned.get("country"),
                notes="Mapped automatically from matching guardian details.",
            )
            group = create_group(payload, actor_id)
            firestore_sibling_groups.add_document(str(group["id"]), {
                "auto_mapped": True, "matched_by": planned.get("matched_by") or [],
            })
            for member_id in planned["student_ids"]:
                try:
                    group = add_member(group, member_id, actor_id)
                except HTTPException as exc:
                    logger.warning("Auto-map: could not add %s to new group %s: %s",
                                   member_id, group["id"], exc.detail)
            planned["id"] = group["id"]
            created_ids[id(planned)] = int(group["id"])
            result["group_id"] = int(group["id"])
            result.pop("planned_group", None)

        elif result.get("action") == "added":
            group_id = result.get("group_id")
            if group_id is None:
                # Joined a group planned earlier in this sweep, now written. The planner's
                # `created` step already added every planned member, so this is a no-op
                # beyond recording the id.
                planned = result.pop("planned_group_ref", None)
                group_id = created_ids.get(id(planned)) if planned is not None else None
                if group_id is not None:
                    result["group_id"] = int(group_id)
                    continue
            if group_id is None:
                result["action"] = "conflict"
                result["reason"] = "The household this student was to join was not created."
                continue
            group = firestore_sibling_groups.get_document(str(group_id))
            if not group:
                result["action"] = "conflict"
                result["reason"] = f"Household {group_id} no longer exists."
                continue
            try:
                add_member(group, result["student_id"], actor_id)
                result["group_id"] = int(group_id)
            except HTTPException as exc:
                result["action"] = "conflict"
                result["reason"] = str(exc.detail)

    return results


def auto_map(student_ids: list[int] | None = None, actor_id: int | None = None,
             dry_run: bool = False) -> dict:
    """
    Builds households from guardian details, for every ungrouped student or a named few.

    Safe to run repeatedly: a student already in a household is reported and left alone,
    and a run that would only create the same groups again creates nothing. `dry_run`
    returns exactly what the real run would do, so the office can read the report before
    letting it write.
    """
    snapshot = _snapshot()
    if student_ids:
        wanted = {int(s) for s in student_ids}
        targets = [s for s in snapshot["students"] if int(s["id"]) in wanted]
    else:
        targets = list(snapshot["students"])

    results = _plan_with_joins(snapshot, targets)
    if not dry_run:
        results = apply_auto_map(results, actor_id)

    by_action: dict[str, list] = {}
    for result in results:
        by_action.setdefault(result["action"], []).append(result)

    return {
        "dry_run": dry_run,
        "considered": len(results),
        "created": by_action.get("created", []),
        "added": by_action.get("added", []),
        "conflicts": by_action.get("conflict", []),
        "unmatched": by_action.get("no_match", []),
        "skipped": by_action.get("skipped", []),
        "already_grouped": len(by_action.get("already_grouped", [])),
        "summary": {action: len(rows) for action, rows in by_action.items()},
    }


def _plan_with_joins(snapshot: dict, targets: list[dict]) -> list[dict]:
    """
    `plan_auto_map`, with each `added`-to-a-planned-group result carrying a reference to
    the planned group so the apply step can resolve its id after the write.
    """
    planned_before = {id(g): g for g in snapshot["groups"]}
    results = plan_auto_map(snapshot, targets)
    # Any group in the snapshot that was not there before was planned by this run. An
    # `added` result naming no id joined one of them; find which by membership.
    planned_groups = [g for g in snapshot["groups"] if id(g) not in planned_before]
    for result in results:
        if result.get("action") == "added" and result.get("group_id") is None:
            for group in planned_groups:
                if result["student_id"] in _member_ids(group):
                    result["planned_group_ref"] = group
                    break
    return results


def auto_place(student_id, actor_id: int | None = None) -> dict | None:
    """
    The hook: puts one student into a household if their guardian details decide it.

    Called after a student is created, after their guardian details change, and after a
    parent login is linked to them. Never raises - a failure here must not fail the
    admission that triggered it - and returns the result for the caller to log or show.
    """
    try:
        report = auto_map([int(student_id)], actor_id)
    except Exception:  # pragma: no cover - a mapping failure must never break admission
        logger.exception("Automatic family mapping failed for student %s.", student_id)
        return None

    for bucket in ("created", "added", "conflicts", "unmatched", "skipped"):
        for result in report.get(bucket) or []:
            if result["action"] in ("created", "added"):
                logger.info("Auto-mapped student %s into household %s (%s).",
                            student_id, result.get("group_id"), result.get("group_name"))
            return result
    return None
