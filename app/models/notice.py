"""
The notice board.

One collection serves both products and every audience, because a notice is the same object
whoever reads it: a title, a body, a window it is live for, and a rule saying who it reaches.
Splitting it per audience would have meant four near-identical collections and four copies of
the "is this still current?" logic.

Read state is a separate collection rather than a list of user ids on the notice. A
school-wide notice reaches every account, and appending a thousand ids to one document is
both a write-contention problem and, eventually, a document-size one.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.enums import NoticeAudience, NoticePriority, NoticeStatus


@dataclass(slots=True)
class Notice:
    """
    One posted notice.

    The three target lists are read selectively: `audience` decides which one applies and the
    others are ignored. That is why a notice always has exactly one answer to "who gets
    this?" even after an author has edited it three times and left stale ids behind in the
    lists they are no longer using.

    `publish_at` and `expires_at` are the clock half of visibility, and `status` is the
    author's half - a PUBLISHED notice with a future `publish_at` is scheduled, not live.
    Keeping them separate is what makes "post this on results day" possible without anybody
    having to be at a keyboard on results day.
    """
    title: str
    body: str

    program: str = "LMS"
    audience: NoticeAudience = NoticeAudience.EVERYONE
    status: NoticeStatus = NoticeStatus.DRAFT
    priority: NoticePriority = NoticePriority.NORMAL

    # Read only when `audience` names them; see the class docstring.
    target_roles: list[str] = field(default_factory=list)
    target_class_ids: list[int] = field(default_factory=list)
    target_user_ids: list[int] = field(default_factory=list)

    # Whether a CLASS notice also reaches the staff who teach those classes. Usually yes -
    # a teacher blindsided by an announcement their own class has already read is the
    # complaint this flag exists to prevent - but a notice about fee arrears should not go
    # to the staff room.
    include_class_teachers: bool = True
    # Whether it also reaches the parents linked to the students it targets.
    include_parents: bool = False

    publish_at: datetime | None = None
    expires_at: datetime | None = None

    # Sticks to the top of the board regardless of date, for the handful of standing notices
    # (term dates, emergency numbers) that must not scroll away.
    is_pinned: bool = False

    # [{"file_name", "file_url", "content_type", "size_bytes"}]
    attachments: list[dict[str, Any]] = field(default_factory=list)

    # Whether an email went out as well as the in-app post. Recorded so a re-publish does not
    # mail everybody a second time.
    email_sent_at: datetime | None = None
    notify_by_email: bool = False

    created_by: int | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class NoticeRead:
    """
    A receipt: this user opened this notice.

    Written at most once per pair, with a derived document id (`{notice_id}:{user_id}`) so a
    second read is an idempotent overwrite rather than a duplicate row. That is what lets the
    unread badge be a count of "targeted minus receipts" without a de-duplication pass.
    """
    notice_id: int
    user_id: int
    read_at: datetime = field(default_factory=datetime.utcnow)
    # Set when the reader dismissed it explicitly rather than merely opening the board.
    dismissed: bool = False
    id: str | None = None
