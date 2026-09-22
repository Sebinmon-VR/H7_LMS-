"""
Request and response shapes for the notice board.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.enums import (
    NoticeAudience, NoticePriority, NoticeStatus, Program, UserRole,
)


class NoticeCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1, max_length=20000)

    program: Program = Program.LMS
    audience: NoticeAudience = NoticeAudience.EVERYONE
    priority: NoticePriority = NoticePriority.NORMAL
    # Created as a draft by default. Posting to the whole school is not something that should
    # happen because a form defaulted to it; the author publishes deliberately.
    status: NoticeStatus = NoticeStatus.DRAFT

    target_roles: list[UserRole] = Field(default_factory=list)
    target_class_ids: list[int] = Field(default_factory=list)
    target_user_ids: list[int] = Field(default_factory=list)

    include_class_teachers: bool = True
    include_parents: bool = False

    publish_at: datetime | None = Field(
        None, description="Leave empty to go live the moment it is published."
    )
    expires_at: datetime | None = None
    is_pinned: bool = False
    notify_by_email: bool = False
    attachments: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _targets_match_audience(self):
        """
        A targeted notice must actually name targets.

        Checked here rather than at publish time because an empty target list is a typing
        mistake, not a decision, and the cheapest place to catch it is the keystroke that
        made it. The alternative - a CLASS notice with no classes - saves cleanly, looks
        published, and reaches nobody, which is the single hardest notice-board bug to
        notice.
        """
        required = {
            NoticeAudience.ROLE: ("target_roles", "at least one role"),
            NoticeAudience.CLASS: ("target_class_ids", "at least one class"),
            NoticeAudience.USER: ("target_user_ids", "at least one user"),
        }.get(self.audience)

        if required and not getattr(self, required[0]):
            raise ValueError(
                f"An audience of {self.audience.value} needs {required[1]} "
                f"in '{required[0]}'."
            )

        if self.expires_at and self.publish_at and self.expires_at <= self.publish_at:
            raise ValueError("expires_at must fall after publish_at")
        return self


class NoticeUpdate(BaseModel):
    """
    Partial update; omitted fields are left unchanged.

    No audience cross-check here, unlike creation: an author legitimately switches audience
    and targets in two separate saves, and rejecting the first of them would make the edit
    impossible. The publish step re-checks instead, which is the moment it actually matters.
    """
    title: str | None = Field(None, min_length=1, max_length=200)
    body: str | None = Field(None, min_length=1, max_length=20000)
    program: Program | None = None
    audience: NoticeAudience | None = None
    priority: NoticePriority | None = None
    status: NoticeStatus | None = None
    target_roles: list[UserRole] | None = None
    target_class_ids: list[int] | None = None
    target_user_ids: list[int] | None = None
    include_class_teachers: bool | None = None
    include_parents: bool | None = None
    publish_at: datetime | None = None
    expires_at: datetime | None = None
    is_pinned: bool | None = None
    notify_by_email: bool | None = None
    attachments: list[dict[str, Any]] | None = None


class NoticeOut(BaseModel):
    id: int
    title: str
    body: str
    program: str
    audience: NoticeAudience
    status: NoticeStatus
    priority: NoticePriority

    target_roles: list[str] = Field(default_factory=list)
    target_class_ids: list[int] = Field(default_factory=list)
    target_user_ids: list[int] = Field(default_factory=list)
    # Resolved names, so an admin reviewing the board is not reading a list of integers.
    target_class_names: list[str] = Field(default_factory=list)

    include_class_teachers: bool = True
    include_parents: bool = False

    publish_at: datetime | None = None
    expires_at: datetime | None = None
    is_pinned: bool = False
    notify_by_email: bool = False
    email_sent_at: datetime | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)

    # Derived from the clock at read time, never stored: whether this notice is on the board
    # right now. See `NoticeStatus` for why it cannot be the status field.
    is_live: bool = False
    is_scheduled: bool = False
    is_expired: bool = False

    # Present on a reader's own feed; null on the admin listing where "read by whom?" has no
    # single answer.
    is_read: bool | None = None
    read_at: datetime | None = None
    # Present on the admin listing: how many of the targeted accounts have opened it.
    read_count: int | None = None
    recipient_count: int | None = None

    author_id: int | None = None
    author_name: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class NoticeFeed(BaseModel):
    """
    A reader's board in one response.

    `unread_count` is returned alongside the items rather than left to the client to count,
    because the badge has to stay correct when the list is paginated or filtered, and a
    client counting what it can see gets a different - smaller - number every time.
    """
    items: list[NoticeOut] = Field(default_factory=list)
    unread_count: int = 0
    pinned_count: int = 0


class NoticePublishResult(BaseModel):
    notice: NoticeOut
    recipient_count: int
    emails_sent: int = 0
    detail: str
