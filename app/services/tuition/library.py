"""
The tuition library: books, notes and recordings shared between admins, teachers and students.

The brief asks for something the LMS's study-materials module cannot express: **all three
roles upload**. That single change is what makes this a separate module rather than a filter
over the existing one, because the moment students can publish, two questions appear that a
teacher-only uploader never had to answer.

**Who sees it?** In a one-to-one product the default audience is two people, so ENROLLMENT is
the default visibility - a worksheet a teacher files against Priya's physics goes to Priya
and nobody else. Widening to SUBJECT or PROGRAM is what actually builds a library, and it is
a deliberate act rather than a side effect of uploading.

**Has anyone looked at it?** Visibility is the uploader's *intent*; approval is whether that
intent has been honoured. Splitting them is what lets a student share notes with the whole
programme without a student being able to publish to the whole programme unreviewed. Teacher
and admin uploads are approved on arrival. A student's PRIVATE upload needs no review because
it reaches nobody; anything wider waits for a teacher.

Read access is computed per viewer rather than stored per item, because an item's audience
changes when a student's enrollments change and a stored audience list would go stale
silently - which, for a privacy boundary, is the wrong way to fail.
"""

import logging
from datetime import datetime

from fastapi import HTTPException, UploadFile

from app.core.enums import LibraryApprovalStatus, LibraryVisibility, UserRole
from app.core.firebase import (
    firestore_tuition_library, hydrate_tuition_library_item, prefetch_tuition,
)
from app.core.gcp_services import StorageError, storage_service
from app.services.tuition.common import is_admin, is_teacher, now_utc, store_dt, user_id_of
from app.services.tuition.enrollments import require_enrollment, visible_to
from app.services.tuition.settings_store import tuition_settings

logger = logging.getLogger("tuition.library")

MATERIAL_TYPES = frozenset({"NOTES", "BOOK", "RECORDING", "LINK", "WORKSHEET", "QUESTION_PAPER"})


# ---------------------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------------------

def _viewer_scope(user) -> tuple[set[int], set[int]]:
    """
    The enrollment ids and subject ids a viewer is connected to.

    One query, reused by every visibility test for that request. Resolving it per item would
    turn listing a library of two hundred books into two hundred enrollment queries.
    """
    enrollments = visible_to(user, include_inactive=True)
    return (
        {e.get("id") for e in enrollments if e.get("id") is not None},
        {e.get("subject_id") for e in enrollments if e.get("subject_id") is not None},
    )


def may_view_item(item: dict, user, scope: tuple[set[int], set[int]] | None = None) -> bool:
    """
    Whether one person may see one item.

    The order of the checks is the policy. An uploader always sees their own upload, however
    it was moderated - a student whose notes were rejected must still be able to find and fix
    them. Everything after that requires the item to be approved, so a pending upload is
    invisible to its intended audience until somebody has looked at it.
    """
    if is_admin(user):
        return True

    viewer_id = user_id_of(user)
    if item.get("uploaded_by") == viewer_id:
        return True

    if item.get("approval_status") != LibraryApprovalStatus.APPROVED.value:
        return False

    if viewer_id in (item.get("shared_with_user_ids") or []):
        return True

    enrollment_ids, subject_ids = scope if scope is not None else _viewer_scope(user)
    visibility = item.get("visibility")

    if visibility == LibraryVisibility.PROGRAM.value:
        return True
    if visibility == LibraryVisibility.SUBJECT.value:
        return item.get("subject_id") in subject_ids
    if visibility == LibraryVisibility.ENROLLMENT.value:
        return item.get("enrollment_id") in enrollment_ids
    # PRIVATE, and the uploader check above already failed.
    return False


def visible_items(user, subject_id: int | None = None, enrollment_id=None,
                  material_type: str | None = None, search: str | None = None,
                  include_pending: bool = False) -> list[dict]:
    """
    Everything a viewer may see, filtered.

    `include_pending` is for the moderation queue: a teacher reviewing student uploads needs
    to see exactly the items the visibility rules are currently hiding, so it is a separate
    flag rather than a widening of the rules themselves.
    """
    scope = _viewer_scope(user)
    results = []
    for item in firestore_tuition_library.list_all():
        if include_pending:
            if not _may_moderate_item(item, user, scope):
                continue
            if item.get("approval_status") != LibraryApprovalStatus.PENDING.value:
                continue
        elif not may_view_item(item, user, scope):
            continue

        if subject_id is not None and item.get("subject_id") != subject_id:
            continue
        if enrollment_id is not None and str(item.get("enrollment_id")) != str(enrollment_id):
            continue
        if material_type and item.get("material_type") != material_type:
            continue
        if search:
            needle = search.strip().lower()
            haystack = " ".join(str(item.get(f) or "") for f in ("title", "description"))
            haystack += " " + " ".join(str(t) for t in (item.get("tags") or []))
            if needle not in haystack.lower():
                continue
        results.append(item)

    return sorted(results, key=lambda i: str(i.get("uploaded_at") or ""), reverse=True)


def _may_moderate_item(item: dict, user, scope: tuple[set[int], set[int]]) -> bool:
    """
    Who may approve a pending upload.

    Admins anywhere; a teacher only for items connected to the students they teach. A teacher
    reviewing uploads from a subject they do not take is not moderation, it is browsing other
    people's material, and the difference matters when the material is a student's own notes.
    """
    if is_admin(user):
        return True
    if not is_teacher(user):
        return False
    _, subject_ids = scope
    enrollment_ids, _ = scope
    if item.get("enrollment_id") in enrollment_ids:
        return True
    if item.get("subject_id") in subject_ids:
        return True
    return item.get("visibility") == LibraryVisibility.PROGRAM.value


def require_item(item_id) -> dict:
    item = firestore_tuition_library.get_document(str(item_id))
    if not item:
        raise HTTPException(status_code=404, detail=f"Library item {item_id} not found")
    return item


def hydrate_many(items: list[dict]) -> list[dict]:
    if not items:
        return []
    prefetch_tuition(items)
    return [hydrate_tuition_library_item(item) for item in items]


# ---------------------------------------------------------------------------------------
# Uploading
# ---------------------------------------------------------------------------------------

def _initial_approval(user, visibility: str) -> str:
    """
    Whether an upload is live immediately or waits for review.

    A student's PRIVATE upload reaches nobody, so reviewing it would be moderation theatre;
    anything wider waits. The requirement can be switched off entirely by an admin, for a
    programme that would rather trust its students and deal with problems after the fact.
    """
    if is_admin(user) or is_teacher(user):
        return LibraryApprovalStatus.APPROVED.value
    if visibility == LibraryVisibility.PRIVATE.value:
        return LibraryApprovalStatus.APPROVED.value
    if not tuition_settings()["student_uploads_need_approval"]:
        return LibraryApprovalStatus.APPROVED.value
    return LibraryApprovalStatus.PENDING.value


def _assert_may_target(user, enrollment_id, visibility: str) -> None:
    """
    Stops an upload being filed against somebody else's private class.

    Without this, any tuition user could attach material to any enrollment id and have it
    delivered to that student - a straightforward way to reach a child you do not teach.
    Admins are exempt; everybody else must be on the arrangement they are filing against.
    """
    if enrollment_id is None or is_admin(user):
        return
    enrollment = require_enrollment(enrollment_id)
    if user_id_of(user) not in {enrollment.get("student_id"), enrollment.get("teacher_id")}:
        raise HTTPException(
            status_code=403,
            detail="You can only share material against a tuition arrangement you are part of.",
        )


async def store_upload(file: UploadFile, payload, user) -> dict:
    """
    Saves an uploaded file and records it in the library.

    The upload is attempted against the configured cloud provider and falls back to local
    disk with a warning, exactly as LMS materials do - a teacher whose Drive is misconfigured
    still gets their book saved, and the warning says why it is not where they expected.
    """
    visibility = payload.visibility.value if hasattr(payload.visibility, "value") else str(payload.visibility)
    _assert_may_target(user, payload.enrollment_id, visibility)

    folder = f"tuition/{'subject_' + str(payload.subject_id) if payload.subject_id else 'general'}"
    try:
        stored = await storage_service.save_file_detailed(file=file, folder=folder)
    except StorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return _persist(
        payload, user, visibility,
        file_url=stored["url"],
        storage_provider=stored["provider"],
        storage_warning=stored.get("warning"),
        file_name=file.filename,
    )


def store_link(payload, user) -> dict:
    """
    Records a link rather than a file.

    A textbook on a publisher's site, a YouTube playlist, a recording that lives in Drive
    already. Uploading a copy of something that is already on the web is worse for everybody:
    it goes stale, it costs storage, and it loses the source.
    """
    visibility = payload.visibility.value if hasattr(payload.visibility, "value") else str(payload.visibility)
    _assert_may_target(user, payload.enrollment_id, visibility)
    if not payload.external_url:
        raise HTTPException(status_code=400, detail="external_url is required when sharing a link.")
    return _persist(payload, user, visibility, external_url=payload.external_url)


def _persist(payload, user, visibility: str, **extra) -> dict:
    role = getattr(user, "role", None)
    item_id = firestore_tuition_library.get_next_numeric_id()
    approval = _initial_approval(user, visibility)

    document = {
        "title": payload.title,
        "description": payload.description,
        "material_type": (payload.material_type or "NOTES").upper(),
        "subject_id": payload.subject_id,
        "enrollment_id": payload.enrollment_id,
        "shared_with_user_ids": payload.shared_with_user_ids or [],
        "visibility": visibility,
        "approval_status": approval,
        "approved_by": user_id_of(user) if approval == LibraryApprovalStatus.APPROVED.value else None,
        "approved_at": store_dt(now_utc()) if approval == LibraryApprovalStatus.APPROVED.value else None,
        "tags": [t.strip() for t in (payload.tags or []) if str(t).strip()],
        "uploaded_by": user_id_of(user),
        "uploader_role": role.value if isinstance(role, UserRole) else str(role or ""),
        "download_count": 0,
        "uploaded_at": datetime.utcnow().isoformat(),
        "file_url": None,
        "external_url": None,
        "storage_provider": None,
        "storage_warning": None,
        "file_name": None,
        **extra,
    }

    if document["material_type"] not in MATERIAL_TYPES:
        document["material_type"] = "NOTES"

    firestore_tuition_library.add_document(str(item_id), document)
    document["id"] = item_id
    return document


# ---------------------------------------------------------------------------------------
# Moderation and maintenance
# ---------------------------------------------------------------------------------------

def moderate(item: dict, user, approve: bool, reason: str | None = None) -> dict:
    """Approves or rejects a pending upload. Teachers and admins only - see `_may_moderate_item`."""
    if not _may_moderate_item(item, user, _viewer_scope(user)):
        raise HTTPException(status_code=403, detail="You may not moderate this item.")

    updates = {
        "approval_status": (
            LibraryApprovalStatus.APPROVED.value if approve else LibraryApprovalStatus.REJECTED.value
        ),
        "approved_by": user_id_of(user),
        "approved_at": store_dt(now_utc()),
        "rejection_reason": None if approve else reason,
        "updated_at": datetime.utcnow().isoformat(),
    }
    firestore_tuition_library.add_document(str(item["id"]), updates)
    return {**item, **updates}


def update_item(item: dict, user, payload) -> dict:
    """
    Edits an item's metadata.

    Widening the audience of an already-approved *student* upload re-opens it for review: an
    item approved for one class is not thereby approved for the whole programme, and without
    this the approval step would be trivially bypassed by uploading narrow and editing wide.
    """
    if not (is_admin(user) or item.get("uploaded_by") == user_id_of(user)):
        raise HTTPException(status_code=403, detail="Only the uploader or an admin may edit this item.")

    changed = payload.model_dump(exclude_unset=True)
    updates: dict = {}
    for field in ("title", "description", "material_type", "subject_id", "tags",
                  "shared_with_user_ids", "external_url"):
        if field in changed:
            updates[field] = changed[field]

    if "visibility" in changed and changed["visibility"] is not None:
        value = changed["visibility"]
        new_visibility = value.value if hasattr(value, "value") else str(value)
        updates["visibility"] = new_visibility
        widened = _RANK[new_visibility] > _RANK.get(str(item.get("visibility")), 0)
        uploader_is_student = str(item.get("uploader_role")) == UserRole.STUDENT.value
        if widened and uploader_is_student and not (is_admin(user) or is_teacher(user)):
            if tuition_settings()["student_uploads_need_approval"]:
                updates["approval_status"] = LibraryApprovalStatus.PENDING.value
                updates["approved_by"] = None
                updates["approved_at"] = None

    if not updates:
        return item

    updates["updated_at"] = datetime.utcnow().isoformat()
    firestore_tuition_library.add_document(str(item["id"]), updates)
    return {**item, **updates}


# How wide each visibility reaches, for the "did this edit widen the audience?" test.
_RANK = {
    LibraryVisibility.PRIVATE.value: 0,
    LibraryVisibility.ENROLLMENT.value: 1,
    LibraryVisibility.SUBJECT.value: 2,
    LibraryVisibility.PROGRAM.value: 3,
}


def delete_item(item: dict, user) -> None:
    if not (is_admin(user) or item.get("uploaded_by") == user_id_of(user)):
        raise HTTPException(status_code=403, detail="Only the uploader or an admin may delete this item.")
    firestore_tuition_library.delete_document(str(item["id"]))


def record_download(item: dict) -> dict:
    """
    Counts an access.

    Read-modify-write rather than an atomic increment: the count is a popularity signal for
    the library view, not an accounting figure, and a lost update under concurrent downloads
    of the same book costs nothing worth a transaction.
    """
    count = int(item.get("download_count") or 0) + 1
    firestore_tuition_library.add_document(str(item["id"]), {"download_count": count})
    return {**item, "download_count": count}
