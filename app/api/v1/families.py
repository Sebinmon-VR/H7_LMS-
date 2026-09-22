"""
Sibling groups, parent accounts, and the parent's own view.

Two routers in one module because they are two halves of one feature and splitting them
across files would separate the permission flags from the endpoint that enforces them.

  * `/admin/families/...` - the office records households and grants parents access.
  * `/parent/...`         - what a signed-in parent may actually read.

The parent side is deliberately thin. It resolves *which* children a parent may open and
gates each aspect; it does not re-implement the student endpoints. A parent viewing
attendance gets the same computation the student's own screen does, which is the only way the
two can be guaranteed to agree - and a parent being shown a different attendance percentage
from their child is a phone call the school cannot win.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.v1.dependencies import require_admin, require_roles
from app.core.enums import UserRole
from app.core.firebase import firestore_users, require_document
from app.schemas.family import (
    ChildSummary, FamilyAutoMapReport, FamilyAutoMapRequest, FamilyMatchesOut,
    ParentCreate, ParentLinkCreate, ParentLinkOut, ParentLinkUpdate,
    SiblingGroupCreate, SiblingGroupOut, SiblingGroupUpdate, SiblingMemberAdd,
)
from app.schemas.user import UserOut
from app.services import accounts as account_service
from app.services import families as service

admin_router = APIRouter(prefix="/admin/families", tags=["Families (Admin)"])
parent_router = APIRouter(prefix="/parent", tags=["Parent Module"])

# Parents reach their own module; admins reach it too, so support staff can see exactly what
# a parent is seeing when one rings up about it.
require_parent = require_roles([UserRole.PARENT, UserRole.ADMIN])


# ---------------------------------------------------------------------------------------
# Sibling groups
# ---------------------------------------------------------------------------------------

@admin_router.post("/groups", response_model=SiblingGroupOut,
                   status_code=status.HTTP_201_CREATED)
def create_group(payload: SiblingGroupCreate, admin: UserOut = Depends(require_admin)):
    """
    [Admin Only] Record a household.

    A student may belong to exactly one household; adding one who already belongs elsewhere
    is refused and the error names the other household. That rule is what makes the sibling
    concession safe to price - a student in two families would be discounted twice.

    Members are held in the order given, oldest first, because sibling concessions are
    almost always "full fee for the eldest, concession for the rest".
    """
    return SiblingGroupOut(**service.present_group(service.create_group(payload, admin.id)))


@admin_router.get("/groups", response_model=List[SiblingGroupOut])
def list_groups(
    search: Optional[str] = Query(None, description="Match on family name"),
    include_inactive: bool = Query(False),
    _: UserOut = Depends(require_admin),
):
    """[Admin Only] Every household, with its members resolved."""
    from app.core.firebase import firestore_sibling_groups

    groups = firestore_sibling_groups.list_all()
    if not include_inactive:
        groups = [g for g in groups if g.get("is_active", True)]
    if search:
        needle = search.strip().lower()
        groups = [g for g in groups if needle in str(g.get("family_name", "")).lower()]

    groups.sort(key=lambda g: str(g.get("family_name") or ""))
    return [SiblingGroupOut(**service.present_group(g)) for g in groups]


@admin_router.get("/groups/{group_id}", response_model=SiblingGroupOut)
def get_group(group_id: int, _: UserOut = Depends(require_admin)):
    """[Admin Only] One household."""
    return SiblingGroupOut(**service.present_group(service.require_group(group_id)))


@admin_router.get("/students/{student_id}/group", response_model=Optional[SiblingGroupOut])
def group_for_student(student_id: int, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] The household a student belongs to, or `null`.

    Null is the normal answer for an only child or a family nobody has linked yet, so it is
    returned rather than a 404 - the admission screen calls this to decide whether to offer
    a concession, and an error for the common case would be noise.
    """
    group = service.group_for_student(student_id)
    return SiblingGroupOut(**service.present_group(group)) if group else None


@admin_router.put("/groups/{group_id}", response_model=SiblingGroupOut)
def update_group(
    group_id: int, payload: SiblingGroupUpdate, admin: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Update a household's details. Partial: omitted fields are left unchanged.

    Membership is not editable here - use the add/remove member endpoints, which check the
    one-household rule that a wholesale list replacement would skip.
    """
    group = service.require_group(group_id)
    return SiblingGroupOut(**service.present_group(service.update_group(group, payload, admin.id)))


@admin_router.post("/groups/{group_id}/members", response_model=SiblingGroupOut)
def add_member(
    group_id: int, payload: SiblingMemberAdd, admin: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Add a child to a household.

    Appended to the end of the list, which is admission order and therefore concession
    order. Refused if the student already belongs to another household.
    """
    group = service.require_group(group_id)
    return SiblingGroupOut(
        **service.present_group(service.add_member(group, payload.student_id, admin.id))
    )


@admin_router.delete("/groups/{group_id}/members/{student_id}", response_model=SiblingGroupOut)
def remove_member(
    group_id: int, student_id: int, admin: UserOut = Depends(require_admin)
):
    """[Admin Only] Remove a child from a household."""
    group = service.require_group(group_id)
    return SiblingGroupOut(
        **service.present_group(service.remove_member(group, student_id, admin.id))
    )


@admin_router.delete("/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_group(group_id: int, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Delete a household.

    The students themselves are untouched; what is lost is the sibling concession, which is
    the pricing decision being made by deleting it.
    """
    service.delete_group(service.require_group(group_id))


# ---------------------------------------------------------------------------------------
# Automatic mapping
# ---------------------------------------------------------------------------------------

@admin_router.post("/auto-map", response_model=FamilyAutoMapReport)
def auto_map_families(payload: FamilyAutoMapRequest, admin: UserOut = Depends(require_admin)):
    """
    [Admin Only] Build households from guardian details.

    Students who share a guardian **phone**, a guardian **email** or a **parent login** are
    siblings: each is added to the household its matches are in, or a new one is created
    from them, named after the shared surname. A shared guardian *name* on its own is not
    acted on - it is returned under `unmatched[].suggestions` for the office to confirm
    through the manual endpoints above.

    Runs on its own after every admission, guardian edit and parent link; call this to
    sweep the whole roll (empty `student_ids`) or to retry a few. Safe to repeat.
    `dry_run=true` returns the same report without writing.

    `conflicts` lists students whose matches sit in *different* households - the one case
    the data cannot decide. Merge or fix those by hand.
    """
    return FamilyAutoMapReport(**service.auto_map(
        payload.student_ids or None, admin.id, dry_run=payload.dry_run
    ))


@admin_router.get("/students/{student_id}/matches", response_model=FamilyMatchesOut)
def family_matches(student_id: int, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Who looks like this student's sibling, and why.

    The manual screen's helper: `strong` is what the sweep would act on, `suggestions` the
    name-only matches it leaves to a person. Each entry carries the household the other
    student is already in, so the screen can offer "add to the Sharma household" directly.
    """
    student = require_document(firestore_users, student_id, "Student")
    return FamilyMatchesOut(**service.find_matches(student))


# ---------------------------------------------------------------------------------------
# Parent accounts
# ---------------------------------------------------------------------------------------

@admin_router.post("/parents", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_parent(payload: ParentCreate, admin: UserOut = Depends(require_admin)):
    """
    [Admin Only] Create a parent login and link it to children in one call.

    Omit `email` to have one generated from the name, exactly as for any other account. Omit
    `password` to create it without a credential, then issue one with
    `POST /admin/users/{user_id}/generate-credentials`.

    The role is fixed to PARENT and cannot be set from the body. Links are created after the
    account exists; a bad `student_id` in `links` fails that link and leaves the account, so
    retry the link rather than the whole call.
    """
    account = account_service.create_account(
        payload,
        role=UserRole.PARENT,
        programs=[p.value for p in payload.programs],
    )

    for link in payload.links or []:
        service.create_link(account["id"], link, admin.id)

    return UserOut(**account)


@admin_router.get("/parents", response_model=List[UserOut])
def list_parents(_: UserOut = Depends(require_admin)):
    """[Admin Only] Every parent account."""
    parents = firestore_users.query_documents("role", "==", UserRole.PARENT.value)
    parents.sort(key=lambda p: str(p.get("full_name") or ""))
    return [UserOut(**p) for p in parents]


@admin_router.post("/parents/{parent_id}/links", response_model=ParentLinkOut,
                   status_code=status.HTTP_201_CREATED)
def create_link(
    parent_id: int, payload: ParentLinkCreate, admin: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Grant a parent access to a student.

    Repeating an existing pair updates that link rather than creating a second one - two
    live links between the same two people would give two answers to "may they see the
    fees?".

    `may_view_fees` is off by default. It is the one aspect a non-paying guardian has no
    business reading, so it is granted deliberately rather than inherited.
    """
    return ParentLinkOut(**service.present_link(service.create_link(parent_id, payload, admin.id)))


@admin_router.get("/parents/{parent_id}/links", response_model=List[ParentLinkOut])
def list_links_for_parent(parent_id: int, _: UserOut = Depends(require_admin)):
    """[Admin Only] Every student one parent can reach."""
    require_document(firestore_users, parent_id, "Parent")
    return [ParentLinkOut(**service.present_link(l))
            for l in service.links_for_parent(parent_id, active_only=False)]


@admin_router.get("/students/{student_id}/parents", response_model=List[ParentLinkOut])
def list_links_for_student(student_id: int, _: UserOut = Depends(require_admin)):
    """[Admin Only] Every parent who can reach one student. The front office's view."""
    require_document(firestore_users, student_id, "Student")
    return [ParentLinkOut(**service.present_link(l))
            for l in service.links_for_student(student_id, active_only=False)]


@admin_router.put("/links/{link_id}", response_model=ParentLinkOut)
def update_link(
    link_id: int, payload: ParentLinkUpdate, admin: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Change what a link grants. Partial: omitted fields are left unchanged.

    Setting `is_primary` clears it on the student's other links, so the scheduled reports
    and fee notices go to exactly one address.
    """
    link = service.require_link(link_id)
    return ParentLinkOut(**service.present_link(service.update_link(link, payload, admin.id)))


@admin_router.delete("/links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_link(link_id: int, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Revoke a parent's access to a student.

    Takes effect on the parent's next request: access is resolved per request rather than
    carried in their token, so there is no window in which a revoked link still works.
    """
    service.delete_link(service.require_link(link_id))


# ---------------------------------------------------------------------------------------
# The parent's own view
# ---------------------------------------------------------------------------------------

@parent_router.get("/children", response_model=List[ChildSummary])
def my_children(parent: UserOut = Depends(require_parent)):
    """
    [Parent] The children this account may open - the switcher.

    Each entry carries its own permission flags so the frontend can hide a tab rather than
    render it and have the request rejected. The primary link sorts first.

    An admin calling this sees an empty list unless they happen to be linked to students
    themselves, which is correct: admins reach students through the admin module.
    """
    return [ChildSummary(**c) for c in service.children_of(parent.id)]


@parent_router.get("/children/{student_id}", response_model=ChildSummary)
def my_child(student_id: int, parent: UserOut = Depends(require_parent)):
    """
    [Parent] One child's summary.

    403 both for a student this parent is not linked to and for one they may not see the
    academics of. The two are deliberately indistinguishable - separating them would let a
    parent discover which student ids exist by reading the error text.
    """
    service.assert_may_view(parent, student_id, "academics")

    for child in service.children_of(parent.id):
        if child["student_id"] == int(student_id):
            return ChildSummary(**child)

    # Reachable when an admin calls this for a student they have no link to: the aspect
    # check waved them through, but there is no child summary to return.
    raise HTTPException(
        status_code=404,
        detail=f"No parent link to student {student_id} for this account.",
    )


@parent_router.get("/children/{student_id}/profile", response_model=UserOut)
def my_child_profile(student_id: int, parent: UserOut = Depends(require_parent)):
    """
    [Parent] A child's full profile.

    Gated on the academics flag: a guardian who may not see how the child is doing has no
    reason to read their record either.
    """
    service.assert_may_view(parent, student_id, "academics")
    return UserOut(**require_document(firestore_users, student_id, "Student"))
