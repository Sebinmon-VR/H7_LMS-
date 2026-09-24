"""
Online admission requests.

Two routers on purpose. The public one is what the school's website calls - no login, no
token, open to any origin - and it can do exactly two things: ask what the form should offer,
and file a request. The admin one is where the office reads the queue and decides. Keeping
them apart means nothing an anonymous caller can reach returns a name, a phone number or a
decision, however the URL is guessed.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request, status

from app.api.v1.dependencies import require_admin
from app.core.enums import AdmissionRequestStatus, Program
from app.schemas.admission import (
    AdmissionOptionsOut, AdmissionRequestAdmit, AdmissionRequestAdmitResult,
    AdmissionRequestCreate, AdmissionRequestNoteCreate, AdmissionRequestOut,
    AdmissionRequestStatusUpdate, AdmissionRequestSubmitted, AdmissionRequestSummary,
)
from app.schemas.user import UserOut
from app.services import admission_requests as service

# The path prefix the public-CORS handling in `app.main` keys on. Anything under it answers
# to any origin; nothing under it requires or reads a token.
PUBLIC_PREFIX = "/admissions/requests"

public_router = APIRouter(prefix=PUBLIC_PREFIX, tags=["Admissions - Online requests"])
admin_router = APIRouter(
    prefix="/admin/admissions/requests", tags=["Admissions - Online requests (Admin)"]
)


def _client_key(request: Request) -> str | None:
    """The caller's address for the rate limit, honouring the App Service proxy header."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.client.host if request.client else None


# ---------------------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------------------

@public_router.get("/options", response_model=AdmissionOptionsOut)
def admission_form_options(
    program: Program = Query(Program.LMS, description="Which product the form applies to"),
):
    """
    [Public] What the website form should offer.

    Whether applications are being taken, the session they land in, and the classes on
    offer - names and ids only. No login needed, and nothing here names a person.
    """
    return AdmissionOptionsOut(**service.options(program.value))


@public_router.post("", response_model=AdmissionRequestSubmitted,
                    status_code=status.HTTP_201_CREATED)
def submit_admission_request(
    payload: AdmissionRequestCreate,
    request: Request,
    program: Program = Query(Program.LMS),
):
    """
    [Public] File an admission request from the website.

    Writes one record for the office to review and emails the family an acknowledgement
    with a reference number. Creates no account of any kind. Refused with 409 when an open
    request already exists for the same child, with 429 when one address sends too many,
    and with 503 when the school has switched online requests off.
    """
    return AdmissionRequestSubmitted(**service.submit(payload, program.value, _client_key(request)))


# ---------------------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------------------

@admin_router.get("", response_model=List[AdmissionRequestOut])
def list_admission_requests(
    request_status: Optional[AdmissionRequestStatus] = Query(None, alias="status"),
    program: Optional[Program] = Query(None),
    class_id: Optional[int] = Query(None),
    academic_year_id: Optional[int] = Query(None),
    _: UserOut = Depends(require_admin),
):
    """[Admin Only] The queue, newest first. Every filter is optional."""
    rows = service.list_requests(
        status=request_status.value if request_status else None,
        program=program.value if program else None,
        class_id=class_id,
        academic_year_id=academic_year_id,
    )
    return [AdmissionRequestOut(**service.present(r)) for r in rows]


@admin_router.get("/summary", response_model=AdmissionRequestSummary)
def admission_requests_summary(
    program: Optional[Program] = Query(None), _: UserOut = Depends(require_admin)
):
    """[Admin Only] How many requests sit in each state - the badge on the menu."""
    return AdmissionRequestSummary(**service.summary(program.value if program else None))


@admin_router.get("/{request_id}", response_model=AdmissionRequestOut)
def get_admission_request(request_id: int, _: UserOut = Depends(require_admin)):
    """[Admin Only] One request in full, with its notes and history."""
    return AdmissionRequestOut(**service.present(service.require_request(request_id)))


@admin_router.post("/{request_id}/status", response_model=AdmissionRequestOut)
def update_admission_request_status(
    request_id: int, payload: AdmissionRequestStatusUpdate,
    admin: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Mark a request under review, waitlist it, decline it, or reopen it.

    `notify_applicant` emails the primary contact for WAITLISTED and REJECTED, with `note`
    included verbatim. An admitted request cannot be moved; admitting has its own endpoint.
    """
    updated = service.set_status(
        service.require_request(request_id), payload.status.value, admin,
        note=payload.note, notify=payload.notify_applicant,
    )
    return AdmissionRequestOut(**service.present(updated))


@admin_router.post("/{request_id}/notes", response_model=AdmissionRequestOut)
def add_admission_request_note(
    request_id: int, payload: AdmissionRequestNoteCreate,
    admin: UserOut = Depends(require_admin),
):
    """[Admin Only] An internal note on the request. Never shown to the family."""
    updated = service.add_note(service.require_request(request_id), payload.body, admin)
    return AdmissionRequestOut(**service.present(updated))


@admin_router.post("/{request_id}/admit", response_model=AdmissionRequestAdmitResult)
def admit_admission_request(
    request_id: int, payload: AdmissionRequestAdmit,
    admin: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Admit: create the student account, enrol them, and close the request.

    Uses the class and session the family applied for unless overridden. Optionally creates
    a parent login for the primary contact, issues passwords and emails them, and writes to
    the family. The response carries the new student, the parent (if any), any passwords
    issued - once, never stored - and any step that did not go to plan under `warnings`.
    """
    return AdmissionRequestAdmitResult(**service.admit(service.require_request(request_id), payload, admin))


@admin_router.delete("/{request_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_admission_request(request_id: int, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Remove a request. A student created from it is a separate record and stays.
    """
    service.delete_request(service.require_request(request_id))
