"""
Support: tickets with a message thread, and the contact details for the phone half.

The brief asks for support by chat and by phone, per product. This is the chat half built as
a ticket rather than live messaging, and that is a deliberate trade. A ticket survives the
browser closing, can be picked up by whoever is on duty rather than whoever happened to be
online, and leaves a record of what was promised. Live chat needs a connection held open,
which the hosting this runs on does not guarantee, and loses the conversation the moment
either side navigates away.

The phone half needs no records at all - it is a number and an opening time in
`support_contacts`, one document per program, which is why it lives here as three small
functions rather than a module of its own.

**Internal notes are part of the same thread.** A team that cannot talk privately on a ticket
will talk somewhere else, and then the ticket stops being the record. `is_internal` marks a
message the reporter never sees, and `visible_messages` is the single place that filter is
applied - every read path goes through it, so a note cannot leak by being returned from a
path that forgot.

**`first_response_at` is stamped once.** It is what any response-time measurement is built
on, and recomputing it from the thread would change it every time somebody edited a message.
"""

import logging
from datetime import datetime

from fastapi import HTTPException

from app.core.enums import (
    ACTIVE_TICKET_STATES, Program, TicketCategory, TicketPriority, TicketStatus, UserRole,
)
from app.core.firebase import (
    firestore_support_contacts, firestore_support_tickets, firestore_users, require_document,
)

logger = logging.getLogger("support")

# Who counts as support staff: they see internal notes, may be assigned tickets, and may
# change status. Teachers are included because in a small school the class teacher is the
# first line for an academic question, and routing those through an administrator adds a day.
STAFF_ROLES = frozenset({UserRole.ADMIN, UserRole.TEACHER, UserRole.CLASS_TEACHER})


def _now() -> str:
    return datetime.utcnow().isoformat()


def is_staff(user) -> bool:
    return getattr(user, "role", None) in STAFF_ROLES


def is_admin(user) -> bool:
    return getattr(user, "role", None) == UserRole.ADMIN


def require_ticket(ticket_id) -> dict:
    return require_document(firestore_support_tickets, ticket_id, "Support ticket")


# ---------------------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------------------

def may_view(ticket: dict, user) -> bool:
    """
    Who may open a ticket.

    The person who raised it, and any staff member. Staff see every ticket rather than only
    their assigned ones, because an unassigned queue that nobody can see is a queue nobody
    works.
    """
    if is_staff(user):
        return True
    return int(ticket.get("raised_by", -1)) == int(getattr(user, "id"))


def assert_may_view(ticket: dict, user) -> None:
    if not may_view(ticket, user):
        # 404 rather than 403: a reporter should not be able to discover that a ticket exists
        # by its id.
        raise HTTPException(status_code=404, detail="Support ticket not found")


def visible_messages(ticket: dict, user) -> list[dict]:
    """
    The thread as this viewer sees it, with internal notes filtered for non-staff.

    The single place that filter lives; see the module docstring. Every read path calls this
    rather than reading `messages` directly.
    """
    messages = ticket.get("messages") or []
    if is_staff(user):
        return messages
    return [m for m in messages if not m.get("is_internal")]


# ---------------------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------------------

def _message(user, body: str, attachments=None, is_internal: bool = False) -> dict:
    role = getattr(user, "role", None)
    return {
        "author_id": int(getattr(user, "id")),
        "author_name": getattr(user, "full_name", None),
        "author_role": role.value if hasattr(role, "value") else str(role or ""),
        "body": body,
        "attachments": attachments or [],
        # A non-staff user cannot file an internal note however the request was shaped;
        # trusting the flag from the body would let a reporter hide a message from the team
        # that is meant to read it.
        "is_internal": bool(is_internal) and is_staff(user),
        "sent_at": _now(),
    }


def create_ticket(payload, actor) -> dict:
    """
    Raises a ticket, with the reporter's first message as the opening entry of the thread.

    The body becomes a message rather than a separate `description` field, so the whole
    conversation is one list and rendering it needs no special case for the first entry.
    """
    program = getattr(payload.program, "value", payload.program)

    if not contact_for(program)["tickets_enabled"]:
        raise HTTPException(
            status_code=403,
            detail="Support tickets are turned off. Please use the phone or email contact "
                   "shown on the support page.",
        )

    if payload.about_student_id is not None:
        require_document(firestore_users, payload.about_student_id, "Student")

    ticket_id = firestore_support_tickets.get_next_numeric_id()
    document = {
        "subject": payload.subject.strip(),
        "raised_by": int(actor.id),
        "program": program,
        "category": getattr(payload.category, "value", payload.category),
        "priority": getattr(payload.priority, "value", payload.priority),
        "status": TicketStatus.OPEN.value,
        "messages": [_message(actor, payload.body, payload.attachments)],
        "assigned_to": None,
        "about_student_id": payload.about_student_id,
        "created_at": _now(),
    }
    firestore_support_tickets.add_document(str(ticket_id), document)
    document["id"] = ticket_id
    logger.info("Support ticket %s raised by %s (%s).",
                ticket_id, actor.id, document["category"])
    return document


def reply(ticket: dict, payload, actor) -> dict:
    """
    Appends to the thread.

    A staff reply that is not internal stamps `first_response_at` if nothing has yet, and
    moves an OPEN ticket to IN_PROGRESS - the reporter has been answered, and a queue that
    still shows it as untouched is measuring the wrong thing.

    A reporter replying to a ticket that was waiting on them moves it back to the team's
    queue, which is the whole point of having WAITING_ON_USER as a distinct state.
    """
    if ticket.get("status") == TicketStatus.CLOSED.value:
        raise HTTPException(
            status_code=409,
            detail="This ticket is closed. Raise a new one if you still need help.",
        )

    message = _message(actor, payload.body, payload.attachments, payload.is_internal)
    messages = list(ticket.get("messages") or []) + [message]

    updates = {"messages": messages, "updated_at": _now()}

    if is_staff(actor) and not message["is_internal"]:
        if not ticket.get("first_response_at"):
            updates["first_response_at"] = message["sent_at"]
        if ticket.get("status") == TicketStatus.OPEN.value:
            updates["status"] = TicketStatus.IN_PROGRESS.value
    elif not is_staff(actor):
        if ticket.get("status") in (TicketStatus.WAITING_ON_USER.value,
                                    TicketStatus.RESOLVED.value):
            updates["status"] = TicketStatus.IN_PROGRESS.value

    firestore_support_tickets.add_document(str(ticket["id"]), updates)
    return {**ticket, **updates}


def update_ticket(ticket: dict, payload, actor) -> dict:
    """
    [Staff] Changes status, priority, category or assignee.

    Resolving stamps `resolved_at` and closing stamps `closed_at`, each only once. Reopening
    a resolved ticket clears `resolved_at`, because a ticket resolved twice has two different
    resolution times and any report over them has to pick one arbitrarily.
    """
    if not is_staff(actor):
        raise HTTPException(
            status_code=403, detail="Only staff may change a ticket's status or assignment."
        )

    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    for field in ("status", "priority", "category"):
        if field in updates:
            updates[field] = getattr(updates[field], "value", updates[field])

    if "assigned_to" in updates and updates["assigned_to"] is not None:
        assignee = require_document(firestore_users, updates["assigned_to"], "Assignee")
        if str(assignee.get("role")) not in {r.value for r in STAFF_ROLES}:
            raise HTTPException(
                status_code=400,
                detail=f"{assignee.get('full_name')} is not staff and cannot be assigned "
                       "a support ticket.",
            )
        updates["assigned_to"] = int(updates["assigned_to"])

    new_status = updates.get("status")
    if new_status == TicketStatus.RESOLVED.value and not ticket.get("resolved_at"):
        updates["resolved_at"] = _now()
    if new_status == TicketStatus.CLOSED.value and not ticket.get("closed_at"):
        updates["closed_at"] = _now()
        updates.setdefault("resolved_at", ticket.get("resolved_at") or _now())
    if new_status in ACTIVE_TICKET_STATES and ticket.get("resolved_at"):
        # Reopened. See the docstring.
        updates["resolved_at"] = None
        updates["closed_at"] = None

    updates["updated_at"] = _now()
    firestore_support_tickets.add_document(str(ticket["id"]), updates)
    return {**ticket, **updates}


def rate(ticket: dict, payload, actor) -> dict:
    """
    The reporter's satisfaction score, once the ticket has been resolved.

    Only the person who raised it may rate it, and only after resolution - a score given
    while the problem is still open measures impatience rather than service.
    """
    if int(ticket.get("raised_by", -1)) != int(actor.id):
        raise HTTPException(
            status_code=403, detail="Only the person who raised a ticket may rate it."
        )
    if ticket.get("status") not in (TicketStatus.RESOLVED.value, TicketStatus.CLOSED.value):
        raise HTTPException(
            status_code=409,
            detail="You can rate this ticket once it has been resolved.",
        )

    updates = {
        "satisfaction_rating": int(payload.rating),
        "satisfaction_comment": payload.comment,
        "updated_at": _now(),
    }
    firestore_support_tickets.add_document(str(ticket["id"]), updates)
    return {**ticket, **updates}


# ---------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------

def list_tickets(user, status: str | None = None, program: str | None = None,
                 category: str | None = None, assigned_to=None,
                 mine_only: bool = False) -> list[dict]:
    """
    The ticket list, newest activity first.

    A non-staff user always gets only their own, whatever the filters say.
    """
    tickets = firestore_support_tickets.list_all()

    if not is_staff(user) or mine_only:
        tickets = [t for t in tickets if int(t.get("raised_by", -1)) == int(user.id)]

    if status:
        tickets = [t for t in tickets if t.get("status") == str(status).upper()]
    if program:
        tickets = [t for t in tickets
                   if (t.get("program") or Program.LMS.value) == str(program).upper()]
    if category:
        tickets = [t for t in tickets if t.get("category") == str(category).upper()]
    if assigned_to is not None:
        tickets = [t for t in tickets if str(t.get("assigned_to")) == str(assigned_to)]

    # Urgency first among the ones still needing attention, then most recently touched.
    rank = {TicketPriority.URGENT.value: 0, TicketPriority.HIGH.value: 1,
            TicketPriority.NORMAL.value: 2, TicketPriority.LOW.value: 3}
    tickets.sort(key=lambda t: str(t.get("updated_at") or t.get("created_at") or ""),
                 reverse=True)
    tickets.sort(key=lambda t: (
        t.get("status") not in ACTIVE_TICKET_STATES,
        rank.get(t.get("priority"), 2),
    ))
    return tickets


def present(ticket: dict, user) -> dict:
    """One ticket, with the thread filtered for this viewer."""
    reporter = firestore_users.get_document(str(ticket.get("raised_by"))) or {}
    assignee = (
        firestore_users.get_document(str(ticket["assigned_to"]))
        if ticket.get("assigned_to") is not None else None
    )
    student = (
        firestore_users.get_document(str(ticket["about_student_id"]))
        if ticket.get("about_student_id") is not None else None
    )

    messages = visible_messages(ticket, user)
    return {
        "id": int(ticket["id"]),
        "subject": ticket.get("subject"),
        "raised_by": int(ticket["raised_by"]),
        "raised_by_name": reporter.get("full_name"),
        "raised_by_role": reporter.get("role"),
        "program": ticket.get("program") or Program.LMS.value,
        "category": ticket.get("category") or TicketCategory.OTHER.value,
        "priority": ticket.get("priority") or TicketPriority.NORMAL.value,
        "status": ticket.get("status") or TicketStatus.OPEN.value,
        "messages": messages,
        "message_count": len(messages),
        "assigned_to": ticket.get("assigned_to"),
        "assigned_to_name": (assignee or {}).get("full_name"),
        "about_student_id": ticket.get("about_student_id"),
        "about_student_name": (student or {}).get("full_name"),
        "first_response_at": ticket.get("first_response_at"),
        "resolved_at": ticket.get("resolved_at"),
        "closed_at": ticket.get("closed_at"),
        "resolution_note": ticket.get("resolution_note"),
        "satisfaction_rating": ticket.get("satisfaction_rating"),
        "created_at": ticket.get("created_at"),
        "updated_at": ticket.get("updated_at"),
    }


def queue_summary(program: str | None = None) -> dict:
    """The counts an admin dashboard shows: how much is waiting, and how much is overdue."""
    tickets = firestore_support_tickets.list_all()
    if program:
        tickets = [t for t in tickets
                   if (t.get("program") or Program.LMS.value) == str(program).upper()]

    by_status: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for ticket in tickets:
        status = ticket.get("status") or TicketStatus.OPEN.value
        category = ticket.get("category") or TicketCategory.OTHER.value
        by_status[status] = by_status.get(status, 0) + 1
        by_category[category] = by_category.get(category, 0) + 1

    active = [t for t in tickets if t.get("status") in ACTIVE_TICKET_STATES]
    return {
        "total": len(tickets),
        "active": len(active),
        "unassigned": sum(1 for t in active if t.get("assigned_to") is None),
        "awaiting_first_response": sum(
            1 for t in active if not t.get("first_response_at")
        ),
        "urgent": sum(1 for t in active if t.get("priority") == TicketPriority.URGENT.value),
        "by_status": by_status,
        "by_category": by_category,
    }


# ---------------------------------------------------------------------------------------
# Contact details - the 'call' half
# ---------------------------------------------------------------------------------------

def contact_for(program: str = Program.LMS.value) -> dict:
    """
    The support contact details for a product.

    Per product because a tuition parent and a school parent are usually given different
    numbers, and one shared number reaching the wrong desk is worse than two.

    Returns a fully-populated dict with nulls rather than raising when nothing has been
    configured: a support page that has not been filled in should render empty, not error.
    """
    key = str(program).lower()
    stored = firestore_support_contacts.get_document(key) or {}
    return {
        "program": str(program).upper(),
        "phone": stored.get("phone"),
        "alternate_phone": stored.get("alternate_phone"),
        "whatsapp": stored.get("whatsapp"),
        "email": stored.get("email"),
        "hours": stored.get("hours"),
        "address": stored.get("address"),
        "notes": stored.get("notes"),
        "tickets_enabled": bool(stored.get("tickets_enabled", True)),
    }


def save_contact(program: str, payload, actor_id: int | None = None) -> dict:
    """Merges an admin's changes into a product's contact details."""
    updates = payload.model_dump(exclude_unset=True, exclude_none=True)
    if updates:
        updates["updated_at"] = _now()
        updates["updated_by"] = actor_id
        firestore_support_contacts.add_document(str(program).lower(), updates)
    return contact_for(program)
