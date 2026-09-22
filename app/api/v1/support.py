"""
Support tickets, contact details, and the product switcher.

Two concerns, together because both are things a signed-in user reaches from the same corner
of the interface:

  * `/support/...` - raise a ticket, follow the thread, find the phone number.
  * `/me/programs`  - which products this account may use, for the online school/tuition
    dropdown.

The program switcher is a read of the caller's own profile, not a permission grant. Choosing
"Tuition" in a dropdown does not give anybody tuition access - every tuition endpoint checks
`programs` on its own. This endpoint exists so the frontend knows whether to render the
dropdown at all, and what to put in it.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status

from app.api.v1.dependencies import require_admin, require_any_authenticated
from app.core.enums import Program, TicketCategory, TicketStatus, normalize_programs
from app.schemas.user import UserOut
from app.schemas.workflow import (
    SupportContactOut, SupportContactUpdate, TicketCreate, TicketOut, TicketRating,
    TicketReply, TicketUpdate,
)
from app.services import support as service

router = APIRouter(prefix="/support", tags=["Support"])
admin_router = APIRouter(prefix="/admin/support", tags=["Support (Admin)"])
me_router = APIRouter(prefix="/me", tags=["Me"])


# ---------------------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------------------

@router.post("/tickets", response_model=TicketOut, status_code=status.HTTP_201_CREATED)
def raise_ticket(payload: TicketCreate, user: UserOut = Depends(require_any_authenticated)):
    """
    Raise a support ticket.

    Your message becomes the first entry in the thread, so the whole conversation is one list
    rather than a description plus replies.

    403 when an admin has turned tickets off for that product - the response points at the
    phone and email contact instead.
    """
    ticket = service.create_ticket(payload, user)
    return TicketOut(**service.present(ticket, user))


@router.get("/tickets", response_model=List[TicketOut])
def list_tickets(
    status_filter: Optional[TicketStatus] = Query(None, alias="status"),
    program: Optional[Program] = Query(None),
    category: Optional[TicketCategory] = Query(None),
    assigned_to: Optional[int] = Query(None, description="Staff only."),
    mine_only: bool = Query(False),
    user: UserOut = Depends(require_any_authenticated),
):
    """
    Tickets, with the ones still needing attention first and urgent at the top.

    A student or parent sees only their own, whatever the filters say. Staff see the whole
    queue, including unassigned tickets - a queue nobody can see is a queue nobody works.
    """
    tickets = service.list_tickets(
        user,
        status=status_filter.value if status_filter else None,
        program=program.value if program else None,
        category=category.value if category else None,
        assigned_to=assigned_to,
        mine_only=mine_only,
    )
    return [TicketOut(**service.present(t, user)) for t in tickets]


@router.get("/tickets/{ticket_id}", response_model=TicketOut)
def get_ticket(ticket_id: int, user: UserOut = Depends(require_any_authenticated)):
    """
    One ticket and its thread.

    Internal staff notes are filtered out for the person who raised it. 404 rather than 403
    for somebody else's ticket, so an id cannot be used to discover that one exists.
    """
    ticket = service.require_ticket(ticket_id)
    service.assert_may_view(ticket, user)
    return TicketOut(**service.present(ticket, user))


@router.post("/tickets/{ticket_id}/reply", response_model=TicketOut)
def reply_to_ticket(
    ticket_id: int, payload: TicketReply, user: UserOut = Depends(require_any_authenticated)
):
    """
    Add a message to the thread.

    A staff reply moves an OPEN ticket to IN_PROGRESS and stamps the first-response time. A
    reporter replying to a ticket that was waiting on them moves it back into the team's
    queue.

    `is_internal` marks a staff-only note. It is ignored for non-staff, so a reporter cannot
    hide a message from the team meant to read it.
    """
    ticket = service.require_ticket(ticket_id)
    service.assert_may_view(ticket, user)
    updated = service.reply(ticket, payload, user)
    return TicketOut(**service.present(updated, user))


@router.put("/tickets/{ticket_id}", response_model=TicketOut)
def update_ticket(
    ticket_id: int, payload: TicketUpdate, user: UserOut = Depends(require_any_authenticated)
):
    """
    [Staff] Change a ticket's status, priority, category or assignee.

    Resolving and closing each stamp their time once. Reopening clears them - a ticket
    resolved twice has two resolution times, and any report over them would pick one
    arbitrarily.
    """
    ticket = service.require_ticket(ticket_id)
    service.assert_may_view(ticket, user)
    return TicketOut(**service.present(service.update_ticket(ticket, payload, user), user))


@router.post("/tickets/{ticket_id}/rate", response_model=TicketOut)
def rate_ticket(
    ticket_id: int, payload: TicketRating, user: UserOut = Depends(require_any_authenticated)
):
    """
    Rate the help you got, 1-5.

    Only the person who raised the ticket, and only once it has been resolved - a score given
    while the problem is still open measures impatience rather than service.
    """
    ticket = service.require_ticket(ticket_id)
    service.assert_may_view(ticket, user)
    return TicketOut(**service.present(service.rate(ticket, payload, user), user))


# ---------------------------------------------------------------------------------------
# Contact details
# ---------------------------------------------------------------------------------------

@router.get("/contact", response_model=SupportContactOut)
def support_contact(
    program: Program = Query(Program.LMS, description="Which product's support desk."),
    _: UserOut = Depends(require_any_authenticated),
):
    """
    Who to ring, and when - the phone half of support.

    Per product, because a tuition parent and a school parent are usually given different
    numbers. Returns nulls rather than an error when nothing has been configured, so the
    support page renders empty instead of failing.
    """
    return SupportContactOut(**service.contact_for(program.value))


@admin_router.put("/contact", response_model=SupportContactOut)
def update_support_contact(
    payload: SupportContactUpdate,
    program: Program = Query(Program.LMS),
    admin: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Set a product's support phone, WhatsApp, email, hours and address.

    `tickets_enabled` turns the ticket system off for that product, leaving the phone and
    email contact as the only route. Useful for a school that would rather take support by
    phone than staff a queue.
    """
    return SupportContactOut(**service.save_contact(program.value, payload, admin.id))


@admin_router.get("/queue")
def support_queue(
    program: Optional[Program] = Query(None), _: UserOut = Depends(require_admin)
):
    """
    [Admin Only] The support dashboard's counts.

    `awaiting_first_response` and `unassigned` are the two that matter: a ticket nobody has
    answered and a ticket nobody owns are the failures a queue is meant to surface.
    """
    return service.queue_summary(program.value if program else None)


# ---------------------------------------------------------------------------------------
# The product switcher
# ---------------------------------------------------------------------------------------

@me_router.get("/programs")
def my_programs(user: UserOut = Depends(require_any_authenticated)):
    """
    Which products this account may use - the online school / tuition dropdown.

    A read of your own profile, not a permission grant: choosing a product here gives nobody
    access to it, because every tuition endpoint checks `programs` on its own. This exists so
    the frontend knows whether to render the switcher at all and what to put in it - an
    account with one product should see no dropdown rather than a dropdown with one entry.

    Administrators always see both: one admin team runs both products, and an administrator
    locked out of tuition because nobody ticked a box is a support call, not a security win.
    """
    from app.core.enums import UserRole

    if user.role == UserRole.ADMIN:
        available = [Program.LMS.value, Program.TUITION.value]
    else:
        available = normalize_programs(user.programs)

    labels = {
        Program.LMS.value: "School",
        Program.TUITION.value: "Online Tuition",
    }
    return {
        "user_id": user.id,
        "role": user.role.value,
        "programs": [
            {
                "value": value,
                "label": labels.get(value, value),
                "is_default": value == available[0],
            }
            for value in available
        ],
        "default_program": available[0] if available else Program.LMS.value,
        # False when there is nothing to choose between. The frontend hides the control
        # rather than rendering a dropdown with a single option.
        "show_switcher": len(available) > 1,
    }
