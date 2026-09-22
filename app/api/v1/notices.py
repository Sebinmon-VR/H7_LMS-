"""
The notice board: an admin's authoring endpoints and everybody's feed.

Two routers, one service. Authors create, schedule and publish; readers get a feed resolved
by the same `recipients_of` that decides who an email would go to. See
`app.services.notices` for why that must be one function and not two.

Teachers can author notices for classes they lead. That is not a convenience - a class
teacher who has to ask the office to post "bring your PE kit tomorrow" will use WhatsApp
instead, and the board stops being where the school looks.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.v1.dependencies import (
    require_admin, require_any_authenticated, require_teacher,
)
from app.core.enums import (
    NoticeAudience, NoticeStatus, Program, UserRole,
)
from app.schemas.notice import (
    NoticeCreate, NoticeFeed, NoticeOut, NoticePublishResult, NoticeUpdate,
)
from app.schemas.user import UserOut
from app.services import notices as service
from app.services import permissions

admin_router = APIRouter(prefix="/admin/notices", tags=["Notice Board (Admin)"])
router = APIRouter(prefix="/notices", tags=["Notice Board"])


def _assert_may_author(user: UserOut, payload) -> None:
    """
    Whether this user may post to this audience.

    Admins may post anything. A teacher may post only to classes they lead, which is checked
    against `class_teacher_mappings` rather than against the role: TEACHER and CLASS_TEACHER
    are the same guard, and the authority that separates them is per class.

    A teacher posting to EVERYONE, to a role, or to arbitrary users is refused. The board is
    school-wide and a single mis-addressed staff notice reaches every parent.
    """
    if user.role == UserRole.ADMIN:
        return

    audience = getattr(payload, "audience", None)
    audience_value = getattr(audience, "value", audience)

    if audience_value != NoticeAudience.CLASS.value:
        raise HTTPException(
            status_code=403,
            detail="Teachers may only post notices addressed to a class. "
                   "Ask an administrator to post to a wider audience.",
        )

    for class_id in getattr(payload, "target_class_ids", None) or []:
        permissions.assert_leads_class(user, int(class_id), "post a notice to this class")


# ---------------------------------------------------------------------------------------
# Authoring
# ---------------------------------------------------------------------------------------

@admin_router.post("", response_model=NoticeOut, status_code=status.HTTP_201_CREATED)
def create_notice(payload: NoticeCreate, author: UserOut = Depends(require_teacher)):
    """
    Create a notice. Admins may address any audience; teachers only their own classes.

    Created as a DRAFT unless `status` says otherwise - posting to the whole school should
    not happen because a form defaulted to it.

    Set `publish_at` to schedule: a PUBLISHED notice with a future `publish_at` sits waiting
    and appears on the board by itself. Set `expires_at` to have it come down on its own.
    """
    _assert_may_author(author, payload)
    notice = service.create_notice(payload, author.id)
    return NoticeOut(**service.present(notice, with_counts=True))


@admin_router.get("", response_model=List[NoticeOut])
def list_notices(
    program: Optional[Program] = Query(None),
    status_filter: Optional[NoticeStatus] = Query(None, alias="status"),
    audience: Optional[NoticeAudience] = Query(None),
    with_counts: bool = Query(
        False, description="Include read and recipient counts. Costs two queries per row."
    ),
    author: UserOut = Depends(require_teacher),
):
    """
    Every notice, newest first, whatever its status.

    A teacher sees only the notices they wrote. The board they *read* is `GET /notices/feed`;
    this listing is the authoring view, and one teacher's drafts are not another's business.
    """
    notices = service.list_for_admin(
        program=program.value if program else None,
        status=status_filter.value if status_filter else None,
        audience=audience.value if audience else None,
    )

    if author.role != UserRole.ADMIN:
        notices = [n for n in notices if str(n.get("created_by")) == str(author.id)]

    return [NoticeOut(**service.present(n, with_counts=with_counts)) for n in notices]


@admin_router.get("/{notice_id}", response_model=NoticeOut)
def get_notice(notice_id: int, author: UserOut = Depends(require_teacher)):
    """One notice, with its read and recipient counts."""
    notice = service.require_notice(notice_id)
    if author.role != UserRole.ADMIN and str(notice.get("created_by")) != str(author.id):
        raise HTTPException(status_code=403, detail="This notice was written by somebody else.")
    return NoticeOut(**service.present(notice, with_counts=True))


@admin_router.put("/{notice_id}", response_model=NoticeOut)
def update_notice(
    notice_id: int, payload: NoticeUpdate, author: UserOut = Depends(require_teacher)
):
    """
    Update a notice. Partial: omitted fields are left unchanged.

    Unlike creation, an audience with no targets is accepted here - an author legitimately
    switches audience and targets in two separate saves. The publish step re-checks, which is
    the moment it actually costs something.
    """
    notice = service.require_notice(notice_id)
    if author.role != UserRole.ADMIN and str(notice.get("created_by")) != str(author.id):
        raise HTTPException(status_code=403, detail="This notice was written by somebody else.")

    if payload.audience is not None or payload.target_class_ids is not None:
        merged = type("Merged", (), {
            "audience": payload.audience or notice.get("audience"),
            "target_class_ids": (
                payload.target_class_ids
                if payload.target_class_ids is not None
                else notice.get("target_class_ids")
            ),
        })()
        _assert_may_author(author, merged)

    return NoticeOut(**service.present(service.update_notice(notice, payload, author.id),
                                       with_counts=True))


@admin_router.post("/{notice_id}/publish", response_model=NoticePublishResult)
def publish_notice(notice_id: int, author: UserOut = Depends(require_teacher)):
    """
    Put a notice on the board.

    Returns how many accounts it reaches, so the author can see that "Class 7A" meant 31
    people before anybody reads it.

    A `publish_at` already set is left alone - publishing a notice scheduled for Friday keeps
    it scheduled for Friday rather than posting it now.
    """
    notice = service.require_notice(notice_id)
    if author.role != UserRole.ADMIN and str(notice.get("created_by")) != str(author.id):
        raise HTTPException(status_code=403, detail="This notice was written by somebody else.")

    published = service.publish(notice, author.id)
    recipients = service.recipients_of(published)
    flags = service.visibility_flags(published)

    detail = (
        f"Scheduled for {published.get('publish_at')}; it will appear on the board then."
        if flags["is_scheduled"]
        else f"Live now, on the board for {len(recipients)} account(s)."
    )

    return NoticePublishResult(
        notice=NoticeOut(**service.present(published, with_counts=True)),
        recipient_count=len(recipients),
        emails_sent=0,
        detail=detail,
    )


@admin_router.post("/{notice_id}/archive", response_model=NoticeOut)
def archive_notice(notice_id: int, author: UserOut = Depends(require_teacher)):
    """
    Take a notice down, keeping it for the record.

    The alternative to deleting. An archived notice leaves every reader's board immediately
    but stays legible to whoever needs to know what was announced and when.
    """
    notice = service.require_notice(notice_id)
    if author.role != UserRole.ADMIN and str(notice.get("created_by")) != str(author.id):
        raise HTTPException(status_code=403, detail="This notice was written by somebody else.")
    return NoticeOut(**service.present(service.archive(notice, author.id), with_counts=True))


@admin_router.delete("/{notice_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_notice(notice_id: int, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Delete a notice and its read receipts permanently.

    Admin-only even for a teacher's own notice: archiving is the reversible action, and a
    posted notice is a record of what the school told people. Prefer `/archive`.
    """
    service.delete_notice(service.require_notice(notice_id))


# ---------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------

@router.get("/feed", response_model=NoticeFeed)
def my_feed(
    program: Optional[Program] = Query(
        None, description="Limit to one product's board. Omit for both."
    ),
    unread_only: bool = Query(False),
    limit: Optional[int] = Query(None, ge=1, le=200),
    reader: UserOut = Depends(require_any_authenticated),
):
    """
    The notice board as this user sees it.

    Pinned notices first, then by urgency, then newest. Only notices that are live right now
    appear - drafts, scheduled posts and expired ones are absent.

    `unread_count` counts the whole feed, not the page: a badge computed from a filtered or
    truncated list shrinks every time the user filters, which makes it useless as a badge.
    """
    return NoticeFeed(**service.feed_for(
        reader,
        program=program.value if program else None,
        include_read=not unread_only,
        limit=limit,
    ))


@router.get("/{notice_id}", response_model=NoticeOut)
def read_notice(notice_id: int, reader: UserOut = Depends(require_any_authenticated)):
    """
    Open one notice, recording that this user has read it.

    The receipt is written on the way out, so opening a notice twice is an idempotent
    overwrite rather than a second row.

    404 for a notice that is not live or not addressed to this user - the same answer for
    both, so the endpoint cannot be used to discover that a notice exists.
    """
    notice = service.require_notice(notice_id)

    if not service.is_live(notice) or not service.targets_user(notice, reader):
        raise HTTPException(status_code=404, detail="Notice not found")

    service.mark_read(notice_id, reader.id)
    return NoticeOut(**service.present(notice, viewer_id=reader.id))


@router.post("/{notice_id}/dismiss", response_model=NoticeOut)
def dismiss_notice(notice_id: int, reader: UserOut = Depends(require_any_authenticated)):
    """
    Mark a notice read and dismissed, clearing it from the unread badge.

    Distinct from opening it: a reader who dismisses from the list has decided the headline
    was enough, and recording that separately is what lets an admin tell "31 people read it"
    from "31 people made it go away".
    """
    notice = service.require_notice(notice_id)

    if not service.is_live(notice) or not service.targets_user(notice, reader):
        raise HTTPException(status_code=404, detail="Notice not found")

    service.mark_read(notice_id, reader.id, dismissed=True)
    return NoticeOut(**service.present(notice, viewer_id=reader.id))
