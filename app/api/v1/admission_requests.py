"""
Online admission requests.

Two routers on purpose. The public one is what the school's website calls - no login, no
token, open to any origin - and it can ask what the form should offer, file a request, and,
holding the secret from the family's emails, follow that one application through its
documents and fee (`/portal/{token}`). The admin one is where the office reads the queue and
decides. Keeping them apart means nothing an anonymous caller can reach returns a name, a
phone number or a decision, however the URL is guessed.
"""

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status

from app.api.v1.dependencies import require_admin
from app.core.enums import AdmissionRequestStatus, Program
from app.schemas.admission import (
    AdmissionDocumentsRequest, AdmissionDocumentsReview, AdmissionOptionsOut,
    AdmissionPaymentRequest, AdmissionPaymentReview, AdmissionPipelineConfig, AdmissionPortalOut,
    AdmissionRequestAdmit, AdmissionRequestAdmitResult, AdmissionRequestCreate,
    AdmissionRequestNoteCreate, AdmissionRequestOut, AdmissionRequestStatusUpdate,
    AdmissionRequestSubmitted, AdmissionRequestSummary, AdmissionResendResult,
)
from app.schemas.user import UserOut
from app.services import admission_pipeline as pipeline
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


# ---- The family's portal --------------------------------------------------------------
#
# Every route takes the token from the family's emails. It opens one application and lets
# the page do what the current step needs: upload a document, take one back, submit them,
# send proof of payment. None of these returns file links, notes or anybody else's data.

def _portal(token: str) -> AdmissionPortalOut:
    return AdmissionPortalOut(**pipeline.portal_view(pipeline.require_by_token(token)))


@public_router.get("/portal/{token}", response_model=AdmissionPortalOut)
def admission_portal(token: str):
    """
    [Public, with the family's link] Where the application has got to and what is asked of
    the family. Returning from a Razorpay payment, the fee's status is read back first.
    """
    request = pipeline.require_by_token(token)
    request, _ = pipeline.sync_gateway(request)
    return AdmissionPortalOut(**pipeline.portal_view(request))


# Declared before /documents/{key}, which would otherwise take "submit" for a key.
@public_router.post("/portal/{token}/documents/submit", response_model=AdmissionPortalOut)
def submit_admission_documents(token: str):
    """[Public, with the family's link] Send the uploads to the office for verification."""
    record = pipeline.require_by_token(token)
    return AdmissionPortalOut(**pipeline.portal_view(pipeline.submit_documents(record)))


@public_router.post("/portal/{token}/documents/{key}", response_model=AdmissionPortalOut)
async def upload_admission_document(
    token: str, key: str, request: Request, file: UploadFile = File(...),
):
    """[Public, with the family's link] Add a file (PDF or image) to one requested document."""
    pipeline.assert_upload_rate(_client_key(request))
    record = pipeline.require_by_token(token)
    return AdmissionPortalOut(**pipeline.portal_view(await pipeline.upload_document(record, key, file)))


@public_router.delete("/portal/{token}/documents/{key}/files/{file_id}", response_model=AdmissionPortalOut)
def remove_admission_document_file(token: str, key: str, file_id: str):
    """[Public, with the family's link] Take back a file before the documents are submitted."""
    record = pipeline.require_by_token(token)
    return AdmissionPortalOut(**pipeline.portal_view(pipeline.remove_document_file(record, key, file_id)))


@public_router.post("/portal/{token}/payment", response_model=AdmissionPortalOut)
async def submit_admission_payment(
    token: str,
    request: Request,
    transaction_id: Optional[str] = Form(None),
    paid_on: Optional[date] = Form(None),
    amount: Optional[float] = Form(None),
    method: Optional[str] = Form(None),
    note: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
):
    """
    [Public, with the family's link] Proof of payment: the transaction id, a screenshot, or
    both. A gateway payment that did go through is confirmed instead, with no proof needed.
    """
    pipeline.assert_upload_rate(_client_key(request))
    record = pipeline.require_by_token(token)
    updated = await pipeline.submit_payment_proof(
        record, transaction_id=transaction_id, paid_on=paid_on, amount=amount,
        method=method, note=note, file=file,
    )
    return AdmissionPortalOut(**pipeline.portal_view(updated))


@public_router.post("/portal/{token}/payment/check", response_model=AdmissionPortalOut)
def check_admission_payment(token: str):
    """[Public, with the family's link] Ask the gateway whether the fee has been paid."""
    record = pipeline.require_by_token(token)
    record, _ = pipeline.sync_gateway(record, min_interval=pipeline.EXPLICIT_SYNC_SECONDS)
    return AdmissionPortalOut(**pipeline.portal_view(record))


@public_router.post("/payments/razorpay/webhook", include_in_schema=False)
async def razorpay_webhook(request: Request):
    """Razorpay's `payment_link.paid`, verified by RAZORPAY_WEBHOOK_SECRET."""
    body = await request.body()
    return pipeline.handle_razorpay_webhook(body, request.headers.get("x-razorpay-signature"))


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


@admin_router.get("/config", response_model=AdmissionPipelineConfig)
def admission_pipeline_config(
    program: Program = Query(Program.LMS), _: UserOut = Depends(require_admin)
):
    """[Admin Only] Whether a payment gateway is set up, the school currency, mail status."""
    return AdmissionPipelineConfig(**pipeline.pipeline_config(program.value))


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


@admin_router.post("/{request_id}/documents/request", response_model=AdmissionRequestOut)
def request_admission_documents(
    request_id: int, payload: AdmissionDocumentsRequest, admin: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Select the applicant and ask the family for documents.

    Emails the family a link to their private upload page. The list given becomes the
    checklist; a document already on it keeps its uploads.
    """
    updated = pipeline.request_documents(service.require_request(request_id), payload, admin)
    return AdmissionRequestOut(**service.present(updated))


@admin_router.post("/{request_id}/documents/review", response_model=AdmissionRequestOut)
def review_admission_documents(
    request_id: int, payload: AdmissionDocumentsReview, admin: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Accept or reject each uploaded document.

    Any rejection (with a reason) asks the family to upload those again; every required
    document accepted marks the set verified and tells the family.
    """
    updated = pipeline.review_documents(service.require_request(request_id), payload, admin)
    return AdmissionRequestOut(**service.present(updated))


@admin_router.post("/{request_id}/payment/request", response_model=AdmissionRequestOut)
def request_admission_payment(
    request_id: int, payload: AdmissionPaymentRequest, admin: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Ask for the admission fee.

    With Razorpay configured the payment link is created there and confirms itself once
    paid; otherwise give a payment link from any provider and/or bank or UPI instructions.
    Asking again replaces the previous request (and cancels its Razorpay link).
    """
    updated = pipeline.request_payment(service.require_request(request_id), payload, admin)
    return AdmissionRequestOut(**service.present(updated))


@admin_router.post("/{request_id}/payment/review", response_model=AdmissionRequestOut)
def review_admission_payment(
    request_id: int, payload: AdmissionPaymentReview, admin: UserOut = Depends(require_admin),
):
    """
    [Admin Only] Confirm the fee, or send the family's proof back with a note. Confirming
    works without proof too - money taken at the counter.
    """
    updated = pipeline.review_payment(service.require_request(request_id), payload, admin)
    return AdmissionRequestOut(**service.present(updated))


@admin_router.post("/{request_id}/payment/check", response_model=AdmissionRequestOut)
def check_admission_request_payment(request_id: int, _: UserOut = Depends(require_admin)):
    """[Admin Only] Read the Razorpay link back now; confirms the fee if it was paid."""
    record, _changed = pipeline.sync_gateway(service.require_request(request_id), force=True)
    return AdmissionRequestOut(**service.present(record))


@admin_router.post("/{request_id}/resend", response_model=AdmissionResendResult)
def resend_admission_email(request_id: int, _: UserOut = Depends(require_admin)):
    """[Admin Only] Send the current step's email to the family again."""
    return AdmissionResendResult(**pipeline.resend(service.require_request(request_id)))


@admin_router.delete("/{request_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_admission_request(request_id: int, _: UserOut = Depends(require_admin)):
    """
    [Admin Only] Remove a request. A student created from it is a separate record and stays.
    """
    service.delete_request(service.require_request(request_id))
