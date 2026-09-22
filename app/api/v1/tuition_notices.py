"""
Online tuition - the notice board, on its own routes.

Same collection, same service, same read receipts as the school board; what differs is who
is on either side of it. Every notice authored here is a TUITION notice, so it resolves to
accounts with tuition access and nobody else, and every reader endpoint here is guarded by
`require_tuition_user`, so a school-only account cannot open the tuition board even by id.

Authoring is admin-only. The school board lets a class teacher post to the class they lead;
tuition has no classes, only one-to-one enrollments, and the audience that would correspond -
"my students" - is a USER list the office can address just as well. A CLASS audience is
refused outright on this board for the same reason: the ids it would carry are school
classes, and a tuition notice addressed to Class 7A would reach the school's pupils.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.v1.dependencies import require_admin, require_tuition_user
from app.core.enums import NoticeAudience, NoticeStatus, Program
from app.schemas.notice import (
    NoticeCreate, NoticeFeed, NoticeOut, NoticePublishResult, NoticeUpdate,
)
from app.schemas.user import UserOut
from app.services import notices as service

admin_router = APIRouter(prefix="/admin/tuition/notices",
                         tags=["Tuition - Notice Board (Admin)"])
router = APIRouter(prefix="/tuition/notices", tags=["Tuition - Notice Board"])

TUITION = Program.TUITION.value


def _is_tuition(notice: dict) -> bool:
    return (notice.get("program") or Program.LMS.value) == TUITION


def _require_tuition_notice(notice_id) -> dict:
    """404 for a school notice: it is not on this board, whoever is asking."""
    notice = service.require_notice(notice_id)
    if not _is_tuition(notice):
        raise HTTPException(status_code=404, detail="Notice not found")
    return notice


def _assert_program(payload) -> None:
    """
    A body that names another program on the tuition board is a mistake in the request.

    Checked only when the field was actually sent: `NoticeCreate.program` defaults to LMS,
    and a tuition form that never mentions the field must not be refused for it.
    """
    if "program" in payload.model_fields_set and payload.program is not None \
            and payload.program != Program.TUITION:
        raise HTTPException(
            status_code=400,
            detail="Notices on the tuition board are always TUITION notices. "
                   "Use /admin/notices to post to the school.",
        )


def _assert_audience(audience) -> None:
    if audience == NoticeAudience.CLASS:
        raise HTTPException(
            status_code=400,
            detail="Tuition has no classes to address. Use EVERYONE, ROLE (teachers or "
                   "students across the programme) or USER (named students).",
        )


# ---------------------------------------------------------------------------------------
# Authoring
# ---------------------------------------------------------------------------------------

@admin_router.post("", response_model=NoticeOut, status_code=status.HTTP_201_CREATED)
def create_notice(payload: NoticeCreate, author: UserOut = Depends(require_admin)):
    """
    [Admin Only] Post to the tuition board.

    `program` may be left out - it is TUITION here regardless. Created as a DRAFT unless
    `status` says otherwise; set `publish_at` to schedule and `expires_at` to have it come
    down on its own. `include_parents` reaches the parents linked to the targeted students.
    """
    _assert_program(payload)
    _assert_audience(payload.audience)
    payload.program = Program.TUITION
    notice = service.create_notice(payload, author.id)
    return NoticeOut(**service.present(notice, with_counts=True))


@admin_router.get("", response_model=List[NoticeOut])
def list_notices(
    status_filter: Optional[NoticeStatus] = Query(None, alias="status"),
    audience: Optional[NoticeAudience] = Query(None),
    with_counts: bool = Query(
        False, description="Include read and recipient counts. Costs two queries per row."
    ),
    _: UserOut = Depends(require_admin),
):
    """[Admin Only] Every tuition notice, newest first, whatever its status."""
    notices = service.list_for_admin(
        program=TUITION,
        status=status_filter.value if status_filter else None,
        audience=audience.value if audience else None,
    )
    return [NoticeOut(**service.present(n, with_counts=with_counts)) for n in notices]


@admin_router.get("/{notice_id}", response_model=NoticeOut)
def get_notice(notice_id: int, _: UserOut = Depends(require_admin)):
    """[Admin Only] One tuition notice, with its read and recipient counts."""
    notice = _require_tuition_notice(notice_id)
    return NoticeOut(**service.present(notice, with_counts=True))


@admin_router.put("/{notice_id}", response_model=NoticeOut)
def update_notice(
    notice_id: int, payload: NoticeUpdate, author: UserOut = Depends(require_admin)
):
    """
    [Admin Only] Update a tuition notice. Partial: omitted fields are left unchanged.

    The notice stays on the tuition board: a `program` of LMS is refused rather than moving
    it to the school, where it would reach a different set of people than it was reviewed for.
    """
    notice = _require_tuition_notice(notice_id)
    _assert_program(payload)
    _assert_audience(payload.audience)
    return NoticeOut(**service.present(service.update_notice(notice, payload, author.id),
                                       with_counts=True))


@admin_router.post("/{notice_id}/publish", response_model=NoticePublishResult)
def publish_notice(notice_id: int, author: UserOut = Depends(require_admin)):
    """
    [Admin Only] Put a tuition notice on the board.

    Returns how many tuition accounts it reaches. A `publish_at` already set is left alone,
    so publishing a notice scheduled for Friday keeps it scheduled for Friday.
    """
    notice = _require_tuition_notice(notice_id)
    published = service.publish(notice, author.id)
    recipients = service.recipients_of(published)
    flags = service.visibility_flags(published)

    detail = (
        f"Scheduled for {published.get('publish_at')}; it will appear on the board then."
        if flags["is_scheduled"]
        else f"Live now, on the tuition board for {len(recipients)} account(s)."
    )
    return NoticePublishResult(
        notice=NoticeOut(**service.present(published, with_counts=True)),
        recipient_count=len(recipients),
        emails_sent=0,
        detail=detail,
    )


@admin_router.post("/{notice_id}/archive", response_model=NoticeOut)
def archive_notice(notice_id: int, author: UserOut = Depends(require_admin)):
    """[Admin Only] Take a tuition notice down, keeping it for the record."""
    notice = _require_tuition_notice(notice_id)
    return NoticeOut(**service.present(service.archive(notice, author.id), with_counts=True))


@admin_router.delete("/{notice_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_notice(notice_id: int, _: UserOut = Depends(require_admin)):
    """[Admin Only] Delete a tuition notice and its read receipts. Prefer `/archive`."""
    service.delete_notice(_require_tuition_notice(notice_id))


# ---------------------------------------------------------------------------------------
# Reading - tuition students, teachers and admins
# ---------------------------------------------------------------------------------------

@router.get("/feed", response_model=NoticeFeed)
def my_feed(
    unread_only: bool = Query(False),
    limit: Optional[int] = Query(None, ge=1, le=200),
    reader: UserOut = Depends(require_tuition_user),
):
    """
    The tuition board as this user sees it: pinned first, then by urgency, then newest.

    Only live tuition notices addressed to this account. `unread_count` counts the whole
    board, not the page, so it is safe to use as a badge.
    """
    return NoticeFeed(**service.feed_for(
        reader, program=TUITION, include_read=not unread_only, limit=limit,
    ))


@router.get("/{notice_id}", response_model=NoticeOut)
def read_notice(notice_id: int, reader: UserOut = Depends(require_tuition_user)):
    """
    Open one tuition notice, recording that this user has read it.

    404 for a notice that is not live, not on the tuition board, or not addressed to this
    user - the same answer for all three, so the endpoint cannot be used to discover that a
    notice exists.
    """
    notice = _require_tuition_notice(notice_id)
    if not service.is_live(notice) or not service.targets_user(notice, reader):
        raise HTTPException(status_code=404, detail="Notice not found")
    service.mark_read(notice_id, reader.id)
    return NoticeOut(**service.present(notice, viewer_id=reader.id))


@router.post("/{notice_id}/dismiss", response_model=NoticeOut)
def dismiss_notice(notice_id: int, reader: UserOut = Depends(require_tuition_user)):
    """Mark a tuition notice read and dismissed, clearing it from the unread badge."""
    notice = _require_tuition_notice(notice_id)
    if not service.is_live(notice) or not service.targets_user(notice, reader):
        raise HTTPException(status_code=404, detail="Notice not found")
    service.mark_read(notice_id, reader.id, dismissed=True)
    return NoticeOut(**service.present(notice, viewer_id=reader.id))
