"""
The notice board.

The whole module turns on one function, `recipients_of`. Everything else - the feed, the
unread badge, the optional email, the admin's read-count - is derived from it, and that is
deliberate: a notice board whose "who can see this?" and "who gets emailed about this?" are
computed by two different pieces of code will eventually disagree, and the disagreement shows
up as a parent receiving an email about a notice they cannot open.

Visibility has two independent halves, and both must pass:

  * the **author's** decision - `status` is PUBLISHED, and
  * the **clock** - `publish_at` has arrived and `expires_at` has not.

They are kept apart for the reason `ExamStatus` and `ExamWindowState` are kept apart
elsewhere in this codebase: one field answering both makes "why can nobody see this?"
unanswerable. `visibility_flags` reports each half separately so the admin screen can say
"scheduled for Friday" rather than just "not visible".

Read receipts use a derived document id, `{notice_id}:{user_id}`, so opening a notice twice
is an idempotent overwrite. The unread badge is then a subtraction rather than a
de-duplicating scan.
"""

import logging
from datetime import datetime

from fastapi import HTTPException

from app.core.enums import (
    NoticeAudience, NoticePriority, NoticeStatus, Program, UserRole,
)
from app.core.firebase import (
    firestore_classes, firestore_notice_reads, firestore_notices,
    firestore_student_enrollments, firestore_users, require_document,
)
from app.services import families as family_service

logger = logging.getLogger("notices")

# How the board sorts. Urgency first, then recency - a board ordered purely by date buries
# "school closed tomorrow" under three routine posts made after it.
_PRIORITY_RANK = {
    NoticePriority.URGENT.value: 0,
    NoticePriority.HIGH.value: 1,
    NoticePriority.NORMAL.value: 2,
    NoticePriority.LOW.value: 3,
}


def _now() -> datetime:
    return datetime.utcnow()


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _as_datetime(value) -> datetime | None:
    """
    Parses a stored timestamp, tolerating the shapes Firestore hands back.

    Returns None on anything unreadable rather than raising. A notice with a corrupted
    `expires_at` should stay on the board, not take down every request that lists it.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def require_notice(notice_id) -> dict:
    return require_document(firestore_notices, notice_id, "Notice")


# ---------------------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------------------

def visibility_flags(notice: dict, reference: datetime | None = None) -> dict:
    """
    The three answers an admin screen needs, computed from the clock. Never stored.

    `is_scheduled` and `is_expired` are what turn "this notice is not visible" into a
    sentence somebody can act on.
    """
    now = reference or _now()
    published = notice.get("status") == NoticeStatus.PUBLISHED.value

    publish_at = _as_datetime(notice.get("publish_at"))
    expires_at = _as_datetime(notice.get("expires_at"))

    not_yet = bool(publish_at and now < publish_at)
    expired = bool(expires_at and now >= expires_at)

    return {
        "is_live": published and not not_yet and not expired,
        "is_scheduled": published and not_yet,
        "is_expired": published and expired,
    }


def is_live(notice: dict, reference: datetime | None = None) -> bool:
    return visibility_flags(notice, reference)["is_live"]


# ---------------------------------------------------------------------------------------
# Audience resolution
# ---------------------------------------------------------------------------------------

def _students_in_classes(class_ids) -> set[int]:
    wanted = {int(c) for c in class_ids}
    students = set()
    for enrollment in firestore_student_enrollments.list_all():
        if int(enrollment.get("class_id", -1)) in wanted:
            students.add(int(enrollment["student_id"]))
    return students


def _active_users(program: str) -> list[dict]:
    """
    Every account that could receive a notice for this program.

    A profile with no `programs` field is an LMS account - the same default the rest of the
    codebase uses, and changing it here would hand pre-tuition accounts notices about a
    product they were never given.
    """
    wanted = str(program).upper()
    out = []
    for user in firestore_users.list_all():
        if not user.get("is_active", True):
            continue
        programs = user.get("programs") or [Program.LMS.value]
        if wanted in programs:
            out.append(user)
    return out


def recipients_of(notice: dict) -> set[int]:
    """
    Every user id this notice is addressed to.

    The single source of truth for both the feed and the email sweep; see the module
    docstring for why it must not be computed twice.

    Note the ordering of the parent expansion: parents are added from the *resolved* student
    set, not from the target lists. A ROLE notice aimed at students therefore reaches those
    students' parents too when `include_parents` is set, which is what an author means by the
    checkbox, and it keeps working when the audience is later switched to CLASS.
    """
    program = notice.get("program") or Program.LMS.value
    audience = notice.get("audience") or NoticeAudience.EVERYONE.value

    recipients: set[int] = set()
    students: set[int] = set()

    if audience == NoticeAudience.EVERYONE.value:
        recipients = {int(u["id"]) for u in _active_users(program)}
        students = {
            int(u["id"]) for u in _active_users(program)
            if u.get("role") == UserRole.STUDENT.value
        }

    elif audience == NoticeAudience.ROLE.value:
        wanted_roles = {str(r).upper() for r in notice.get("target_roles") or []}
        for user in _active_users(program):
            if str(user.get("role", "")).upper() in wanted_roles:
                recipients.add(int(user["id"]))
                if user.get("role") == UserRole.STUDENT.value:
                    students.add(int(user["id"]))

    elif audience == NoticeAudience.CLASS.value:
        class_ids = notice.get("target_class_ids") or []
        students = _students_in_classes(class_ids)
        recipients |= students
        if notice.get("include_class_teachers", True):
            recipients |= family_service.teacher_ids_for_classes(class_ids)

    elif audience == NoticeAudience.USER.value:
        for user_id in notice.get("target_user_ids") or []:
            try:
                recipients.add(int(user_id))
            except (TypeError, ValueError):
                continue
        for user_id in list(recipients):
            user = firestore_users.get_document(str(user_id))
            if user and user.get("role") == UserRole.STUDENT.value:
                students.add(user_id)

    if notice.get("include_parents") and students:
        for student_id in students:
            for link in family_service.links_for_student(student_id):
                recipients.add(int(link["parent_id"]))

    # The author always sees their own notice, so a draft is reviewable from the same feed
    # everybody else reads it in rather than only from the admin listing.
    author_id = notice.get("created_by")
    if author_id is not None:
        try:
            recipients.add(int(author_id))
        except (TypeError, ValueError):
            pass

    return recipients


def targets_user(notice: dict, user) -> bool:
    """
    Whether one user is in a notice's audience.

    Resolved by asking `recipients_of` rather than by a cheaper per-user shortcut. The
    shortcut is tempting - "is their class in the list?" - and it is how the two halves of
    this module drift apart: it would have to re-implement the parent expansion, the teacher
    inclusion and the program filter, and miss one of them.
    """
    user_id = int(getattr(user, "id", user))
    return user_id in recipients_of(notice)


# ---------------------------------------------------------------------------------------
# Read receipts
# ---------------------------------------------------------------------------------------

def read_id(notice_id, user_id) -> str:
    return f"{notice_id}:{user_id}"


def mark_read(notice_id, user_id, dismissed: bool = False) -> dict:
    document = {
        "notice_id": int(notice_id),
        "user_id": int(user_id),
        "read_at": _iso(_now()),
        "dismissed": bool(dismissed),
    }
    firestore_notice_reads.add_document(read_id(notice_id, user_id), document)
    return document


def reads_for_user(user_id) -> dict[int, dict]:
    """Every receipt this user holds, keyed by notice id. One query per feed, not per row."""
    receipts = firestore_notice_reads.query_documents("user_id", "==", int(user_id))
    return {int(r["notice_id"]): r for r in receipts if r.get("notice_id") is not None}


def read_count(notice_id) -> int:
    return len(firestore_notice_reads.query_documents("notice_id", "==", int(notice_id)))


# ---------------------------------------------------------------------------------------
# Authoring
# ---------------------------------------------------------------------------------------

def _validate_targets(notice: dict) -> None:
    """
    Re-checks that a targeted notice names targets.

    Run at publish rather than at every save; `NoticeUpdate` deliberately allows the
    half-finished state where the audience has been switched but the new target list has not
    been filled in yet. This is the moment the omission would start costing something.
    """
    audience = notice.get("audience")
    required = {
        NoticeAudience.ROLE.value: ("target_roles", "role"),
        NoticeAudience.CLASS.value: ("target_class_ids", "class"),
        NoticeAudience.USER.value: ("target_user_ids", "user"),
    }.get(audience)

    if required and not notice.get(required[0]):
        raise HTTPException(
            status_code=400,
            detail=f"This notice is addressed to {audience} but names no {required[1]}. "
                   f"Add at least one to '{required[0]}' before publishing.",
        )


def create_notice(payload, actor_id: int | None = None) -> dict:
    for class_id in payload.target_class_ids or []:
        require_document(firestore_classes, class_id, "Class")

    notice_id = firestore_notices.get_next_numeric_id()
    document = {
        "title": payload.title.strip(),
        "body": payload.body,
        "program": getattr(payload.program, "value", payload.program),
        "audience": getattr(payload.audience, "value", payload.audience),
        "status": getattr(payload.status, "value", payload.status),
        "priority": getattr(payload.priority, "value", payload.priority),
        "target_roles": [getattr(r, "value", str(r)) for r in payload.target_roles or []],
        "target_class_ids": [int(c) for c in payload.target_class_ids or []],
        "target_user_ids": [int(u) for u in payload.target_user_ids or []],
        "include_class_teachers": bool(payload.include_class_teachers),
        "include_parents": bool(payload.include_parents),
        "publish_at": _iso(payload.publish_at),
        "expires_at": _iso(payload.expires_at),
        "is_pinned": bool(payload.is_pinned),
        "notify_by_email": bool(payload.notify_by_email),
        "attachments": payload.attachments or [],
        "created_by": actor_id,
        "created_at": _iso(_now()),
    }

    if document["status"] == NoticeStatus.PUBLISHED.value:
        _validate_targets(document)
        document.setdefault("publish_at", None)
        if not document["publish_at"]:
            document["publish_at"] = _iso(_now())

    firestore_notices.add_document(str(notice_id), document)
    document["id"] = notice_id
    logger.info("Created notice %s (%s) for %s.",
                notice_id, document["title"], document["audience"])
    return document


def update_notice(notice: dict, payload, actor_id: int | None = None) -> dict:
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)

    for class_id in updates.get("target_class_ids") or []:
        require_document(firestore_classes, class_id, "Class")

    for field in ("program", "audience", "status", "priority"):
        if field in updates:
            updates[field] = getattr(updates[field], "value", updates[field])
    if "target_roles" in updates:
        updates["target_roles"] = [
            getattr(r, "value", str(r)) for r in updates["target_roles"]
        ]
    for field in ("publish_at", "expires_at"):
        if field in updates:
            updates[field] = _iso(updates[field])

    merged = {**notice, **updates}
    if merged.get("status") == NoticeStatus.PUBLISHED.value:
        _validate_targets(merged)

    publish_at = _as_datetime(merged.get("publish_at"))
    expires_at = _as_datetime(merged.get("expires_at"))
    if publish_at and expires_at and expires_at <= publish_at:
        raise HTTPException(status_code=400, detail="expires_at must fall after publish_at")

    updates["updated_at"] = _iso(_now())
    updates["updated_by"] = actor_id
    firestore_notices.add_document(str(notice["id"]), updates)
    return {**notice, **updates}


def publish(notice: dict, actor_id: int | None = None) -> dict:
    """
    Puts a notice on the board.

    `publish_at` is only defaulted when the author left it empty. A notice scheduled for
    Friday and published on Tuesday stays scheduled for Friday - overwriting the field here
    would silently turn every scheduled post into an immediate one, which is the opposite of
    what pressing "publish" on a scheduled notice means.
    """
    _validate_targets(notice)

    updates = {
        "status": NoticeStatus.PUBLISHED.value,
        "updated_at": _iso(_now()),
        "updated_by": actor_id,
    }
    if not notice.get("publish_at"):
        updates["publish_at"] = _iso(_now())

    firestore_notices.add_document(str(notice["id"]), updates)
    result = {**notice, **updates}
    logger.info("Published notice %s (%s).", notice["id"], notice.get("title"))
    return result


def archive(notice: dict, actor_id: int | None = None) -> dict:
    updates = {
        "status": NoticeStatus.ARCHIVED.value,
        "updated_at": _iso(_now()),
        "updated_by": actor_id,
    }
    firestore_notices.add_document(str(notice["id"]), updates)
    return {**notice, **updates}


def delete_notice(notice: dict) -> None:
    """
    Removes a notice and its receipts.

    The receipts are deleted rather than orphaned because they are keyed by notice id and
    nothing else would ever clean them up; a board that has been in use for three years would
    otherwise carry a receipt collection mostly referring to notices nobody can name.
    """
    for receipt in firestore_notice_reads.query_documents("notice_id", "==", int(notice["id"])):
        firestore_notice_reads.delete_document(str(receipt["id"]))
    firestore_notices.delete_document(str(notice["id"]))
    logger.info("Deleted notice %s (%s).", notice["id"], notice.get("title"))


# ---------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------

def sort_notices(notices: list[dict]) -> list[dict]:
    """
    Board order: pinned first, then urgency, then newest within each band.

    Done as two stable passes rather than one composite key because the timestamp sorts
    descending while the other two sort ascending, and an ISO string cannot be negated the
    way a number can. Python's sort is stable, so ordering by date first and then by the
    ascending keys preserves the date order inside each band - which is the whole trick.
    """
    ordered = sorted(
        notices,
        key=lambda n: str(n.get("publish_at") or n.get("created_at") or ""),
        reverse=True,
    )
    ordered.sort(key=lambda n: (not n.get("is_pinned"),
                                _PRIORITY_RANK.get(n.get("priority"), 2)))
    return ordered


def feed_for(user, program: str | None = None, include_read: bool = True,
             limit: int | None = None) -> dict:
    """
    One reader's board, with the unread count.

    The count is over the whole feed, not the page: a badge computed from a truncated list
    shrinks every time the user filters, which makes it useless as a badge. See `NoticeFeed`.
    """
    user_id = int(getattr(user, "id", user))
    wanted_program = str(program).upper() if program else None

    receipts = reads_for_user(user_id)
    now = _now()

    visible = []
    for notice in firestore_notices.list_all():
        if wanted_program and (notice.get("program") or Program.LMS.value) != wanted_program:
            continue
        if not is_live(notice, now):
            continue
        if user_id not in recipients_of(notice):
            continue
        visible.append(notice)

    unread = sum(1 for n in visible if int(n["id"]) not in receipts)
    pinned = sum(1 for n in visible if n.get("is_pinned"))

    if not include_read:
        visible = [n for n in visible if int(n["id"]) not in receipts]

    visible = sort_notices(visible)

    if limit:
        visible = visible[:limit]

    return {
        "items": [present(n, viewer_id=user_id, receipts=receipts) for n in visible],
        "unread_count": unread,
        "pinned_count": pinned,
    }


def list_for_admin(program: str | None = None, status: str | None = None,
                   audience: str | None = None) -> list[dict]:
    notices = firestore_notices.list_all()

    if program:
        wanted = str(program).upper()
        notices = [n for n in notices if (n.get("program") or Program.LMS.value) == wanted]
    if status:
        wanted_status = str(status).upper()
        notices = [n for n in notices if (n.get("status") or "") == wanted_status]
    if audience:
        wanted_audience = str(audience).upper()
        notices = [n for n in notices if (n.get("audience") or "") == wanted_audience]

    notices.sort(key=lambda n: str(n.get("created_at") or ""), reverse=True)
    return notices


def present(notice: dict, viewer_id: int | None = None,
            receipts: dict[int, dict] | None = None,
            with_counts: bool = False) -> dict:
    """
    One notice as the API returns it.

    `receipts` is passed in by the feed so a board of forty notices costs one receipt query
    rather than forty. `with_counts` is the admin listing's extra round trip and is off by
    default for exactly that reason.
    """
    notice_id = int(notice["id"])
    flags = visibility_flags(notice)

    class_names = []
    for class_id in notice.get("target_class_ids") or []:
        class_room = firestore_classes.get_document(str(class_id))
        if class_room and class_room.get("name"):
            class_names.append(class_room["name"])

    author = None
    if notice.get("created_by") is not None:
        author = firestore_users.get_document(str(notice["created_by"]))

    view = {
        "id": notice_id,
        "title": notice.get("title"),
        "body": notice.get("body"),
        "program": notice.get("program") or Program.LMS.value,
        "audience": notice.get("audience") or NoticeAudience.EVERYONE.value,
        "status": notice.get("status") or NoticeStatus.DRAFT.value,
        "priority": notice.get("priority") or NoticePriority.NORMAL.value,
        "target_roles": notice.get("target_roles") or [],
        "target_class_ids": notice.get("target_class_ids") or [],
        "target_user_ids": notice.get("target_user_ids") or [],
        "target_class_names": class_names,
        "include_class_teachers": bool(notice.get("include_class_teachers", True)),
        "include_parents": bool(notice.get("include_parents")),
        "publish_at": notice.get("publish_at"),
        "expires_at": notice.get("expires_at"),
        "is_pinned": bool(notice.get("is_pinned")),
        "notify_by_email": bool(notice.get("notify_by_email")),
        "email_sent_at": notice.get("email_sent_at"),
        "attachments": notice.get("attachments") or [],
        "author_id": notice.get("created_by"),
        "author_name": (author or {}).get("full_name"),
        "created_at": notice.get("created_at"),
        "updated_at": notice.get("updated_at"),
        **flags,
    }

    if viewer_id is not None:
        receipt = (receipts or {}).get(notice_id)
        if receipts is None:
            receipt = firestore_notice_reads.get_document(read_id(notice_id, viewer_id))
        view["is_read"] = receipt is not None
        view["read_at"] = (receipt or {}).get("read_at")

    if with_counts:
        view["read_count"] = read_count(notice_id)
        view["recipient_count"] = len(recipients_of(notice))

    return view
