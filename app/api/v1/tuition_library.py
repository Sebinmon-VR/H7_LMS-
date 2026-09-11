"""
Online tuition - shared library, and per-user preferences.

One router for all three roles, because the brief asks for one library that admins, teachers
and students all contribute to. Splitting it per role would have meant three copies of the
same visibility rules, and visibility is the part that must not be got wrong twice.

What differs by role is not the route but what the rules let you see and publish:

  * **Anyone** may upload. A student sharing their notes is a feature, not an exception.
  * **Visibility** decides the audience - the default reaches the two people on one
    arrangement, which is the privacy expectation of a private class. Widening to a subject
    or to the whole programme is what actually builds a library.
  * **Approval** decides whether that audience gets it yet. Teacher and admin uploads are
    live on arrival; a student's wait for a teacher, unless they are private and reach
    nobody. Splitting intent from approval is what lets students publish without letting them
    publish unreviewed.

`/tuition/me` lives here too - the timezone every other response is rendered in has to be
settable by the person who knows where they are sitting.
"""

import logging
from typing import List

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status

from app.api.v1.dependencies import require_tuition_user
from app.core.enums import LibraryVisibility
from app.core.firebase import firestore_users
from app.schemas.tuition import (
    LibraryItemOut, LibraryItemUpdate, LibraryLinkCreate, LibraryModeration, TimezoneUpdate,
    TuitionUserSummary,
)
from app.schemas.user import UserOut
from app.services.tuition import library as library_service
from app.services.tuition.common import (
    is_admin, is_teacher, program_timezone, timezone_source, user_timezone,
    valid_timezone,
)
from app.services.tuition.settings_store import tuition_settings

logger = logging.getLogger("tuition_library_api")

router = APIRouter(prefix="/tuition", tags=["Tuition - Library & Preferences"])


class _UploadForm:
    """
    The multipart form behind a file upload.

    A hand-rolled dependency rather than a Pydantic body, because a file upload cannot carry
    a JSON body alongside it - the metadata has to arrive as form fields. This keeps the
    shape declared in one place instead of eight `Form(...)` parameters repeated per route.
    """

    def __init__(
        self,
        title: str = Form(..., max_length=200),
        description: str | None = Form(None, max_length=2000),
        material_type: str = Form("NOTES", description="NOTES | BOOK | RECORDING | WORKSHEET"),
        subject_id: int | None = Form(None),
        enrollment_id: int | None = Form(None),
        visibility: LibraryVisibility = Form(LibraryVisibility.ENROLLMENT),
        tags: str | None = Form(None, description="Comma-separated"),
        shared_with_user_ids: str | None = Form(None, description="Comma-separated user ids"),
    ):
        self.title = title
        self.description = description
        self.material_type = material_type
        self.subject_id = subject_id
        self.enrollment_id = enrollment_id
        self.visibility = visibility
        self.tags = [t.strip() for t in (tags or "").split(",") if t.strip()]
        self.shared_with_user_ids = [
            int(v) for v in (shared_with_user_ids or "").replace(",", " ").split() if v.isdigit()
        ]
        self.external_url = None


# =======================================================================================
# The library
# =======================================================================================

@router.post("/library/upload", response_model=LibraryItemOut,
             status_code=status.HTTP_201_CREATED)
async def upload_material(
    form: _UploadForm = Depends(),
    file: UploadFile = File(...),
    current_user: UserOut = Depends(require_tuition_user),
):
    """
    [Tuition Admin/Teacher/Student] Share a book, note, worksheet or recording.

    Anyone in the programme may upload - that is the point. What varies is reach: the default
    `ENROLLMENT` sends it to the two people on one arrangement, `SUBJECT` to everyone taking
    that subject, `PROGRAM` to the whole tuition library, and `PRIVATE` to nobody but you.

    A student's upload to anything wider than PRIVATE waits for a teacher to approve it, and
    comes back `approval_status: PENDING`. Teacher and admin uploads are live immediately.
    An admin can switch the requirement off entirely in the programme settings.

    The file goes to the configured cloud provider and falls back to local disk with a
    warning rather than failing - your book is saved either way, and `storage_warning` says
    why it is not where you expected.
    """
    record = await library_service.store_upload(file, form, current_user)
    return LibraryItemOut(**library_service.hydrate_many([record])[0])


@router.post("/library/links", response_model=LibraryItemOut,
             status_code=status.HTTP_201_CREATED)
def share_link(payload: LibraryLinkCreate,
               current_user: UserOut = Depends(require_tuition_user)):
    """
    [Tuition Admin/Teacher/Student] Share a link instead of a file.

    A publisher's page, a video, a Drive folder. Better than uploading a copy of something
    already on the web: a copy goes stale, costs storage, and loses the source. Same
    visibility and approval rules as an upload.
    """
    record = library_service.store_link(payload, current_user)
    return LibraryItemOut(**library_service.hydrate_many([record])[0])


@router.get("/library", response_model=List[LibraryItemOut])
def browse_library(
    subject_id: int | None = Query(None),
    enrollment_id: int | None = Query(None),
    material_type: str | None = Query(None),
    search: str | None = Query(None, description="Matches title, description and tags"),
    current_user: UserOut = Depends(require_tuition_user),
):
    """
    [Tuition Admin/Teacher/Student] Everything shared with me.

    Computed per reader rather than stored per item, so what you can see follows your current
    arrangements. A student who takes up chemistry gains the chemistry shelf that afternoon;
    one whose enrollment ends loses it. Storing the audience on the item would make that go
    stale silently, which for a privacy boundary is the wrong way to fail.
    """
    records = library_service.visible_items(
        current_user, subject_id, enrollment_id, material_type, search
    )
    return [LibraryItemOut(**i) for i in library_service.hydrate_many(records)]


@router.get("/library/pending", response_model=List[LibraryItemOut])
def moderation_queue(current_user: UserOut = Depends(require_tuition_user)):
    """
    [Tuition Admin/Teacher] Student uploads waiting for review.

    A teacher sees items connected to the students they teach or the subjects they take;
    an admin sees the lot. Reviewing uploads from a subject you do not teach is not
    moderation, it is reading other people's notes.
    """
    if not (is_admin(current_user) or is_teacher(current_user)):
        raise HTTPException(status_code=403, detail="Only teachers and admins review uploads.")
    records = library_service.visible_items(current_user, include_pending=True)
    return [LibraryItemOut(**i) for i in library_service.hydrate_many(records)]


@router.post("/library/{item_id}/moderate", response_model=LibraryItemOut)
def moderate_item(item_id: int, payload: LibraryModeration,
                  current_user: UserOut = Depends(require_tuition_user)):
    """
    [Tuition Admin/Teacher] Approve or reject a pending upload.

    A rejected item stays visible to whoever uploaded it, with the reason attached, so a
    student can fix it and try again rather than watching their work disappear.
    """
    item = library_service.require_item(item_id)
    updated = library_service.moderate(item, current_user, payload.approve, payload.reason)
    return LibraryItemOut(**library_service.hydrate_many([updated])[0])


@router.get("/library/{item_id}", response_model=LibraryItemOut)
def get_item(item_id: int, current_user: UserOut = Depends(require_tuition_user)):
    item = library_service.require_item(item_id)
    if not library_service.may_view_item(item, current_user):
        # 404 rather than 403: confirming that an item exists but is not for you is itself a
        # small leak, and the id space is guessable.
        raise HTTPException(status_code=404, detail="Library item not found")
    return LibraryItemOut(**library_service.hydrate_many([item])[0])


@router.post("/library/{item_id}/download", response_model=LibraryItemOut)
def register_download(item_id: int, current_user: UserOut = Depends(require_tuition_user)):
    """
    [Tuition Admin/Teacher/Student] Count an access and return the item with its URL.

    The count is a popularity signal for the library view, not an audit trail - the file is
    served from storage directly, so this records intent to open rather than proof of it.
    """
    item = library_service.require_item(item_id)
    if not library_service.may_view_item(item, current_user):
        raise HTTPException(status_code=404, detail="Library item not found")
    updated = library_service.record_download(item)
    return LibraryItemOut(**library_service.hydrate_many([updated])[0])


@router.put("/library/{item_id}", response_model=LibraryItemOut)
def update_item(item_id: int, payload: LibraryItemUpdate,
                current_user: UserOut = Depends(require_tuition_user)):
    """
    [Tuition Admin/Teacher/Student] Edit something I shared.

    Widening an approved student upload's audience sends it back for review - otherwise the
    approval step would be bypassed by uploading narrow and editing wide.
    """
    item = library_service.require_item(item_id)
    updated = library_service.update_item(item, current_user, payload)
    return LibraryItemOut(**library_service.hydrate_many([updated])[0])


@router.delete("/library/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(item_id: int, current_user: UserOut = Depends(require_tuition_user)):
    """[Tuition Admin/Teacher/Student] Remove something I shared. Admins may remove anything."""
    item = library_service.require_item(item_id)
    library_service.delete_item(item, current_user)


# =======================================================================================
# Me
# =======================================================================================

@router.get("/me")
def my_tuition_profile(current_user: UserOut = Depends(require_tuition_user)):
    """
    [Tuition Admin/Teacher/Student] Who I am in the tuition programme, and in what timezone.

    `effective_timezone` is what times are actually rendered in for me, and
    `timezone_source` says which rule decided it:

    - `EXPLICIT`  - I chose it. Nothing overrides this.
    - `DETECTED`  - my browser reported it via the `X-Timezone` header and it was saved.
      This is the usual case for a student sitting outside India, and it follows them if
      they travel.
    - `PROGRAMME` - neither, so I see programme time (Indian by default).

    A client should label times with `effective_timezone` rather than showing a bare hour
    from an unstated zone, which is how people miss classes. When `detected_timezone`
    disagrees with an explicit `timezone`, that is the moment to offer "you seem to be in
    Europe/London - switch?" rather than moving anything without being asked.
    """
    programme = tuition_settings()
    return {
        "user": TuitionUserSummary(**current_user.model_dump()),
        "programme_timezone": programme["timezone"],
        "effective_timezone": str(user_timezone(current_user)),
        "timezone_source": timezone_source(current_user),
        "detected_timezone": current_user.detected_timezone,
        "currency": programme["currency"],
        "default_session_minutes": programme["default_session_minutes"],
        "reminder_minutes_before": programme["reminder_minutes_before"],
        "server_timezone": str(program_timezone()),
    }


@router.put("/me/timezone")
def set_my_timezone(payload: TimezoneUpdate,
                    current_user: UserOut = Depends(require_tuition_user)):
    """
    [Tuition Admin/Teacher/Student] Pin the timezone my times are shown in.

    Only needed to *override* automatic detection - a student outside India already sees
    their own local time from the `X-Timezone` header their browser supplies, with no action
    required. Set this when the browser is wrong, or when somebody wants their times pinned
    to one zone while they travel.

    Validated against the IANA database, so a typo is rejected here rather than silently
    ignored later and leaving them looking at programme time without knowing why. Send `null`
    to unpin and go back to automatic detection.
    """
    zone = valid_timezone(payload.timezone)
    if payload.timezone and not zone:
        raise HTTPException(
            status_code=400,
            detail=f"'{payload.timezone}' is not a known IANA timezone. Use a name like "
                   f"'Asia/Kolkata' or 'Europe/London'.",
        )

    firestore_users.add_document(str(current_user.id), {"timezone": zone})
    current_user.timezone = zone
    return {
        "user_id": current_user.id,
        "timezone": zone,
        "detected_timezone": current_user.detected_timezone,
        "effective_timezone": str(user_timezone(current_user)),
        "timezone_source": timezone_source(current_user),
        "detail": "Times are now pinned to this zone." if zone
                  else "Unpinned. Your times follow the zone your browser reports.",
    }
