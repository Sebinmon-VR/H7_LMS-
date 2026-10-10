"""
What happens to an admission request after the office selects it.

    selected -> documents requested -> uploaded -> verified -> fee requested -> paid
             -> confirmed -> admitted (in `admission_requests.admit`)

The family takes part through one private page on the school website, reached by the link
in every email: `ADMISSION_PORTAL_URL?t=<portal_token>`. The token is the only key - no
login exists yet, since the family has no account until the very last step - so it is long,
random, and opens exactly one application. The page can read that application's progress,
upload into the documents the office asked for, and send proof of payment. Nothing else.

A few rules hold throughout:

* **The office can always skip ahead.** A school that collects documents at the counter
  requests the fee straight away; one that waives the fee admits from "documents verified".
  The statuses describe where a family is, not a gate the admin must pass through.
* **Uploads are private.** They land under `admissions/` in Blob storage, which `/files/`
  refuses to serve; the admin view signs a short-lived link per file. When Blob storage is
  configured but unreachable, an upload is refused rather than falling back to the API's
  local disk, because that disk is served publicly under /uploads.
* **A gateway payment confirms itself.** With Razorpay keys, the fee link is made through
  Razorpay and its status is read back - when the family returns from paying, when the office
  presses "check", and from the webhook. Proof of payment is the fallback for everything
  else: bank transfers, a page that hung, a school with no gateway at all.
* **Emails never fail a step**, exactly as in `admission_requests`.
"""

import logging
import re
import secrets
import threading
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import HTTPException, UploadFile

from app.core import azure_blob, mailer, razorpay
from app.core.config import settings
from app.core.enums import AdmissionRequestStatus, Program
from app.core.firebase import firestore_admission_requests, require_document
from app.core.storage import PROVIDER_BLOB, PROVIDER_LOCAL, storage_service

logger = logging.getLogger("admission_pipeline")

S = AdmissionRequestStatus
NEW, UNDER_REVIEW, WAITLISTED = S.NEW.value, S.UNDER_REVIEW.value, S.WAITLISTED.value
DOCS_REQUESTED, DOCS_SUBMITTED, DOCS_VERIFIED = (
    S.DOCUMENTS_REQUESTED.value, S.DOCUMENTS_SUBMITTED.value, S.DOCUMENTS_VERIFIED.value,
)
PAY_REQUESTED, PAY_SUBMITTED, PAY_VERIFIED = (
    S.PAYMENT_REQUESTED.value, S.PAYMENT_SUBMITTED.value, S.PAYMENT_VERIFIED.value,
)
ADMITTED, REJECTED = S.ADMITTED.value, S.REJECTED.value

# Where documents may be asked for: anywhere before the money. Once a fee is in play,
# rewinding to documents would leave a payment hanging off an application that is no
# longer at that step.
_DOCUMENT_REQUEST_FROM = frozenset({
    NEW, UNDER_REVIEW, WAITLISTED, DOCS_REQUESTED, DOCS_SUBMITTED, DOCS_VERIFIED,
})
# Where the fee may be asked for: any open state short of the money being confirmed.
_PAYMENT_REQUEST_FROM = frozenset({
    NEW, UNDER_REVIEW, WAITLISTED, DOCS_REQUESTED, DOCS_SUBMITTED, DOCS_VERIFIED,
    PAY_REQUESTED, PAY_SUBMITTED,
})

ACCEPTED_EXTENSIONS = {
    ".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp", ".heic": "image/heic", ".heif": "image/heif",
}
ACCEPTED_TYPES = sorted(set(ACCEPTED_EXTENSIONS.values()))

FAMILY = "Family"


def _now() -> str:
    return datetime.utcnow().isoformat()


def _history(status: str, actor, note: str | None) -> dict:
    """The same history row `admission_requests` writes; `actor` None is the family."""
    return {
        "status": status,
        "at": _now(),
        "by": int(actor.id) if actor is not None else None,
        "by_name": getattr(actor, "full_name", None) if actor is not None else FAMILY,
        "note": note,
    }


# One lock per application around every read-modify-write of its documents and payment.
# A family uploading three files at once would otherwise have two of the writes overwrite
# the third's entry. Per process, which covers the realistic case: one family, one browser.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(request_id) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(str(request_id), threading.Lock())


def _reload(request: dict) -> dict:
    return require_document(firestore_admission_requests, request["id"], "Admission request")


def _save(request: dict, updates: dict) -> dict:
    updates = {**updates, "updated_at": _now()}
    firestore_admission_requests.add_document(str(request["id"]), updates)
    return {**request, **updates}


# ---------------------------------------------------------------------------------------
# The family's link
# ---------------------------------------------------------------------------------------

_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]{24,100}$")


def new_token() -> str:
    return secrets.token_urlsafe(32)


def ensure_token(request: dict) -> dict:
    """The request with a portal token, minting and saving one for requests filed before."""
    if request.get("portal_token"):
        return request
    return _save(request, {"portal_token": new_token()})


def portal_url(request: dict) -> str | None:
    token = request.get("portal_token")
    base = (mailer.school_profile().get("admission_portal_url") or "").strip()
    if not token or not base:
        return None
    return f"{base}{'&' if '?' in base else '?'}t={token}"


def require_by_token(token: str) -> dict:
    if not token or not _TOKEN_SHAPE.match(token):
        raise HTTPException(status_code=404, detail=_LINK_INVALID)
    rows = firestore_admission_requests.query_documents("portal_token", "==", token)
    if not rows:
        raise HTTPException(status_code=404, detail=_LINK_INVALID)
    return rows[0]


_LINK_INVALID = (
    "This link is not valid. Please use the most recent email from the admissions office, "
    "or contact the school office for help."
)


# ---------------------------------------------------------------------------------------
# Rate limit for the public uploads
# ---------------------------------------------------------------------------------------

_rate_lock = threading.Lock()
_recent: dict[str, list[float]] = {}
_WINDOW = 3600.0


def assert_upload_rate(client_key: str | None) -> None:
    limit = int(settings.ADMISSION_UPLOAD_RATE_LIMIT or 0)
    if not client_key or limit <= 0:
        return
    now = time.monotonic()
    with _rate_lock:
        recent = [t for t in _recent.get(client_key, []) if now - t < _WINDOW]
        if len(recent) >= limit:
            raise HTTPException(
                status_code=429,
                detail="Too many uploads from this connection. Please try again in an hour, "
                       "or contact the school office.",
            )
        recent.append(now)
        _recent[client_key] = recent
        if len(_recent) > 5000:
            for key in [k for k, v in _recent.items() if not v or now - v[-1] >= _WINDOW]:
                _recent.pop(key, None)


# ---------------------------------------------------------------------------------------
# Emails
# ---------------------------------------------------------------------------------------

def _family(request: dict) -> tuple[str | None, str]:
    return request.get("contact_email"), request.get("contact_name") or "Parent"


def _mail(fn, **kwargs) -> bool:
    """One family email, best-effort. False when mail is off or the send failed."""
    if not kwargs.get("to") or not mailer.is_configured():
        return False
    try:
        return bool(fn(**kwargs))
    except Exception as exc:  # pragma: no cover - never fail a step for its email
        logger.warning("%s not sent: %s", getattr(fn, "__name__", "email"), exc)
        return False


def _alert(request: dict, event: str) -> None:
    from app.services import admission_requests

    admission_requests.alert_office(request, event)


# ---------------------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------------------

def _clean_filename(name: str | None) -> str:
    base = Path(name or "document").name
    base = re.sub(r"[^A-Za-z0-9._ -]+", "_", base).strip(" .") or "document"
    return base[-120:]


async def _read_upload(file: UploadFile) -> tuple[bytes, str, str]:
    """The bytes, a safe name and the content type - or a 400 saying what is wrong."""
    if file is None:
        raise HTTPException(status_code=400, detail="Choose a file to upload.")
    name = _clean_filename(file.filename)
    extension = Path(name).suffix.lower()
    if extension not in ACCEPTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Please upload a PDF or an image (JPG, PNG, WEBP or HEIC).",
        )
    limit = int(settings.ADMISSION_DOCUMENT_MAX_MB) * 1024 * 1024
    data = await file.read(limit + 1)
    if not data:
        raise HTTPException(status_code=400, detail="That file is empty. Please choose another.")
    if len(data) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"That file is larger than {settings.ADMISSION_DOCUMENT_MAX_MB} MB. "
                   f"Please upload a smaller scan or photo.",
        )
    return data, name, ACCEPTED_EXTENSIONS[extension]


def _store(request: dict, folder: str, data: bytes, name: str, content_type: str) -> dict:
    stored = storage_service.save_bytes(
        data, name, folder=f"admissions/{request['id']}/{folder}", content_type=content_type,
    )
    if stored["requested_provider"] == PROVIDER_BLOB and stored["provider"] == PROVIDER_LOCAL:
        # The local fallback is served publicly under /uploads; an identity document must not
        # land there. Take it back and ask the family to try again.
        storage_service.delete_file(stored["url"], PROVIDER_LOCAL)
        logger.error("Admission upload for %s refused: %s", request.get("reference"), stored.get("warning"))
        raise HTTPException(
            status_code=503,
            detail="We could not save the file just now. Please try again in a few minutes.",
        )
    return {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "url": stored["url"],
        "provider": stored["provider"],
        "content_type": content_type,
        "size": len(data),
        "uploaded_at": _now(),
    }


def _signed(url: str | None) -> str | None:
    """A link the office can open: signed for an hour when the file is in Blob storage."""
    blob_name = azure_blob.blob_name_from(url)
    if not blob_name:
        return url
    try:
        return azure_blob.read_url(blob_name, minutes=60)
    except Exception as exc:
        logger.warning("Could not sign a link for '%s': %s", blob_name, exc)
        return url


def _file_view(entry: dict, sign: bool) -> dict:
    view = {k: entry.get(k) for k in ("id", "name", "content_type", "size", "uploaded_at")}
    if sign:
        view["url"] = _signed(entry.get("url"))
    return view


# ---------------------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------------------

def _slug(label: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "document")[:60]


def _document_lines(documents: list[dict]) -> list[str]:
    return [
        doc["label"] + ("" if doc.get("required", True) else " (optional)")
        + (f" - {doc['description']}" if doc.get("description") else "")
        for doc in documents
    ]


def request_documents(request: dict, payload, actor) -> dict:
    """
    Select the applicant and ask for documents. The list given is the checklist from now
    on; a document whose key was already there keeps its uploads and its verdict.
    """
    status = request.get("status") or NEW
    if status not in _DOCUMENT_REQUEST_FROM:
        raise HTTPException(
            status_code=400,
            detail=_cannot(status, "ask for documents"),
        )

    with _lock_for(request["id"]):
        request = ensure_token(_reload(request))
        existing = {doc["key"]: doc for doc in request.get("documents") or []}
        documents, seen = [], set()
        for spec in payload.documents:
            key = _slug(spec.key or spec.label)
            if key in seen:
                raise HTTPException(status_code=400, detail=f"'{spec.label}' is listed twice.")
            seen.add(key)
            kept = existing.get(key) or {"status": "PENDING", "files": []}
            documents.append({
                **kept,
                "key": key,
                "label": spec.label,
                "description": spec.description,
                "required": bool(spec.required),
            })

        note = f"Selected. Documents requested: {', '.join(d['label'] for d in documents)}."
        # Every document on the new list already verified (an optional one dropped, say):
        # nothing is asked of the family, so the application stays verified.
        all_verified = all(d.get("status") == "ACCEPTED" for d in documents)
        new_status = DOCS_VERIFIED if all_verified else DOCS_REQUESTED
        if all_verified:
            note = "Document checklist updated; every document is already verified."
        merged = _save(request, {
            "status": new_status,
            "documents": documents,
            "documents_note": payload.note,
            "documents_deadline": payload.deadline.isoformat() if payload.deadline else None,
            "documents_requested_at": _now(),
            "reviewed_by": int(actor.id),
            "reviewed_by_name": actor.full_name,
            "reviewed_at": _now(),
            "history": [*(request.get("history") or []), _history(new_status, actor, payload.note or note)],
        })

    notified = False
    if payload.notify_applicant and not all_verified:
        notified = _send_documents_request(merged)
    merged["_notified"] = notified
    return merged


def _deadline_label(value) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%d %B %Y").lstrip("0")
    except ValueError:
        return str(value)


def _send_documents_request(request: dict) -> bool:
    to, name = _family(request)
    documents = request.get("documents") or []
    rejected = [d for d in documents if d.get("status") == "REJECTED"]
    if rejected:
        return _mail(
            mailer.send_documents_changes_email, to=to, contact_name=name,
            student_name=request.get("student_full_name") or "",
            reference=request.get("reference") or "",
            problems=[f"{d['label']}: {d.get('review_note') or 'please upload it again'}" for d in rejected],
            portal_url=portal_url(request) or "",
        )
    outstanding = [d for d in documents if d.get("status") not in ("ACCEPTED",)]
    return _mail(
        mailer.send_documents_requested_email, to=to, contact_name=name,
        student_name=request.get("student_full_name") or "",
        reference=request.get("reference") or "",
        class_name=request.get("class_name"), year_name=request.get("academic_year_name"),
        documents=_document_lines(outstanding or documents),
        portal_url=portal_url(request) or "",
        note=request.get("documents_note"),
        deadline=_deadline_label(request.get("documents_deadline")),
    )


def _document(request: dict, key: str) -> dict:
    for doc in request.get("documents") or []:
        if doc.get("key") == key:
            return doc
    raise HTTPException(status_code=404, detail="That document is not on the list for this application.")


def _assert_uploads_open(request: dict) -> None:
    status = request.get("status")
    if status == DOCS_SUBMITTED:
        raise HTTPException(
            status_code=409,
            detail="The documents have been sent for verification. If something needs to "
                   "change, please contact the school office.",
        )
    if status != DOCS_REQUESTED:
        raise HTTPException(status_code=409, detail="Documents are not being collected at this stage.")


async def upload_document(request: dict, key: str, file: UploadFile) -> dict:
    """[Family] Add one file to one requested document."""
    _assert_uploads_open(request)
    doc = _document(request, key)
    if doc.get("status") == "ACCEPTED":
        raise HTTPException(status_code=409, detail=f"{doc['label']} has already been verified.")
    if len(doc.get("files") or []) >= int(settings.ADMISSION_DOCUMENT_MAX_FILES):
        raise HTTPException(
            status_code=400,
            detail=f"{doc['label']} already has {settings.ADMISSION_DOCUMENT_MAX_FILES} files. "
                   f"Remove one before adding another.",
        )

    data, name, content_type = await _read_upload(file)
    entry = _store(request, key, data, name, content_type)

    with _lock_for(request["id"]):
        request = _reload(request)
        _assert_uploads_open(request)
        documents = request.get("documents") or []
        for item in documents:
            if item.get("key") == key:
                item["files"] = [*(item.get("files") or []), entry]
                item["status"] = "UPLOADED"
        return _save(request, {"documents": documents})


def remove_document_file(request: dict, key: str, file_id: str) -> dict:
    """[Family] Take back a file before the documents are sent for verification."""
    _assert_uploads_open(request)
    with _lock_for(request["id"]):
        request = _reload(request)
        _assert_uploads_open(request)
        documents = request.get("documents") or []
        doc = _document(request, key)
        if doc.get("status") == "ACCEPTED":
            raise HTTPException(status_code=409, detail=f"{doc['label']} has already been verified.")
        target = next((f for f in doc.get("files") or [] if f.get("id") == file_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail="That file is not on this document.")
        for item in documents:
            if item.get("key") == key:
                item["files"] = [f for f in item.get("files") or [] if f.get("id") != file_id]
                if not item["files"]:
                    item["status"] = "REJECTED" if item.get("review_note") else "PENDING"
        saved = _save(request, {"documents": documents})
    storage_service.delete_file(target.get("url"), target.get("provider"))
    return saved


def submit_documents(request: dict) -> dict:
    """[Family] Send the uploads to the office. Every required document needs a file."""
    _assert_uploads_open(request)
    with _lock_for(request["id"]):
        request = _reload(request)
        _assert_uploads_open(request)
        documents = request.get("documents") or []
        missing = [
            d["label"] for d in documents
            if d.get("required", True) and d.get("status") not in ("UPLOADED", "ACCEPTED")
        ]
        if missing:
            raise HTTPException(
                status_code=400,
                detail="Please upload " + ", ".join(missing) + " before submitting.",
            )
        if not any(d.get("status") == "UPLOADED" for d in documents):
            raise HTTPException(status_code=400, detail="Upload at least one document before submitting.")
        for doc in documents:
            if doc.get("status") == "UPLOADED":
                doc["status"] = "SUBMITTED"
        merged = _save(request, {
            "status": DOCS_SUBMITTED,
            "documents": documents,
            "documents_submitted_at": _now(),
            "history": [*(request.get("history") or []), _history(DOCS_SUBMITTED, None, "Documents submitted for verification")],
        })

    to, name = _family(merged)
    _mail(mailer.send_documents_received_email, to=to, contact_name=name,
          student_name=merged.get("student_full_name") or "", reference=merged.get("reference") or "",
          portal_url=portal_url(merged))
    _alert(merged, "Documents have been submitted for verification.")
    return merged


def review_documents(request: dict, payload, actor) -> dict:
    """
    [Office] Accept or reject each document. Any rejection sends the family back to the
    upload page with the reasons; every required document accepted verifies the set.
    """
    status = request.get("status")
    if status not in (DOCS_SUBMITTED, DOCS_REQUESTED):
        raise HTTPException(status_code=400, detail=_cannot(status, "review documents"))

    with _lock_for(request["id"]):
        request = _reload(request)
        documents = request.get("documents") or []
        by_key = {d["key"]: d for d in documents}
        for decision in payload.decisions:
            doc = by_key.get(decision.key)
            if doc is None:
                raise HTTPException(status_code=400, detail=f"No document '{decision.key}' on this application.")
            if decision.status == "REJECTED" and not decision.note:
                raise HTTPException(
                    status_code=400,
                    detail=f"Say what is wrong with {doc['label']}, so the family knows what to fix.",
                )
            doc["status"] = decision.status
            doc["review_note"] = decision.note
            doc["reviewed_at"] = _now()
            doc["reviewed_by_name"] = actor.full_name

        rejected = [d for d in documents if d.get("status") == "REJECTED"]
        complete = all(d.get("status") == "ACCEPTED" for d in documents if d.get("required", True))
        updates = {"documents": documents, "reviewed_by": int(actor.id),
                   "reviewed_by_name": actor.full_name, "reviewed_at": _now()}
        if rejected:
            new_status = DOCS_REQUESTED
            note = "Changes requested: " + ", ".join(d["label"] for d in rejected)
        elif complete:
            new_status = DOCS_VERIFIED
            note = "Documents verified"
            updates["documents_verified_at"] = _now()
        else:
            new_status = status
            note = None
        if new_status != status or note:
            updates["status"] = new_status
            updates["history"] = [*(request.get("history") or []),
                                  _history(new_status, actor, payload.note or note)]
        merged = _save(request, updates)

    notified = False
    if payload.notify_applicant and new_status != status:
        to, name = _family(merged)
        if rejected:
            notified = _mail(
                mailer.send_documents_changes_email, to=to, contact_name=name,
                student_name=merged.get("student_full_name") or "",
                reference=merged.get("reference") or "",
                problems=[f"{d['label']}: {d['review_note']}" for d in rejected],
                portal_url=portal_url(merged) or "", note=payload.note,
            )
        else:
            notified = _mail(
                mailer.send_documents_verified_email, to=to, contact_name=name,
                student_name=merged.get("student_full_name") or "",
                reference=merged.get("reference") or "",
                portal_url=portal_url(merged), note=payload.note,
            )
    merged["_notified"] = notified
    return merged


# ---------------------------------------------------------------------------------------
# The fee
# ---------------------------------------------------------------------------------------

def school_currency(program: str = Program.LMS.value) -> str:
    try:
        from app.services.tuition.settings_store import currency_settings

        return currency_settings(program)["base_currency"]
    except Exception:  # pragma: no cover
        return (settings.SCHOOL_CURRENCY or "INR").upper()


def _expiry_epoch(due: date | None) -> int | None:
    """End of the due day, India time, when that is comfortably in the future."""
    if not due:
        return None
    from zoneinfo import ZoneInfo

    from app.services.timetable import school_timezone_name

    try:
        zone = ZoneInfo(school_timezone_name())
    except Exception:
        zone = timezone.utc
    end = datetime(due.year, due.month, due.day, 23, 59, tzinfo=zone)
    epoch = int(end.timestamp())
    return epoch if epoch > time.time() + 3600 else None


def request_payment(request: dict, payload, actor) -> dict:
    """[Office] Ask for the fee, replacing any earlier request."""
    status = request.get("status") or NEW
    if status not in _PAYMENT_REQUEST_FROM:
        raise HTTPException(status_code=400, detail=_cannot(status, "request a payment"))

    program = (request.get("program") or Program.LMS.value).upper()
    currency = (payload.currency or school_currency(program)).upper()
    request = ensure_token(_reload(request))
    previous = request.get("payment") or {}

    use_gateway = bool(payload.use_gateway and razorpay.is_configured() and not payload.payment_link)
    if not use_gateway and not payload.payment_link and not payload.instructions:
        raise HTTPException(
            status_code=400,
            detail="Give the family a payment link or payment instructions (bank or UPI details)"
                   + ("." if razorpay.is_configured() else ", or set up Razorpay to create links automatically."),
        )

    link_url, link_id, gateway_status = payload.payment_link, None, None
    if use_gateway:
        try:
            link = razorpay.create_payment_link(
                amount=payload.amount, currency=currency,
                description=f"{payload.description} - {request.get('student_full_name')} ({request.get('reference')})",
                reference_id=f"{request.get('reference')}-{int(time.time())}",
                customer_name=request.get("contact_name"), customer_email=request.get("contact_email"),
                customer_phone=request.get("contact_phone"),
                callback_url=portal_url(request),
                notes={"request_id": request["id"], "reference": request.get("reference")},
                expire_by=_expiry_epoch(payload.due_date),
            )
        except razorpay.RazorpayError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        link_url, link_id, gateway_status = link.get("short_url"), link.get("id"), link.get("status")

    if previous.get("gateway_link_id") and previous.get("status") != "VERIFIED":
        razorpay.cancel_payment_link(previous["gateway_link_id"])

    payment = {
        "amount": float(payload.amount),
        "currency": currency,
        "description": payload.description,
        "due_date": payload.due_date.isoformat() if payload.due_date else None,
        "link_url": link_url,
        "instructions": payload.instructions,
        "note": payload.note,
        "provider": "RAZORPAY" if use_gateway else "MANUAL",
        "gateway_link_id": link_id,
        "gateway_status": gateway_status,
        "status": "REQUESTED",
        "requested_at": _now(),
        "requested_by_name": actor.full_name,
        # Proof sent against an earlier request is kept: it is evidence, and the office may
        # want it when the family says "but I already paid".
        "submissions": previous.get("submissions") or [],
    }
    with _lock_for(request["id"]):
        merged = _save(_reload(request), {
            "status": PAY_REQUESTED,
            "payment": payment,
            "reviewed_by": int(actor.id),
            "reviewed_by_name": actor.full_name,
            "reviewed_at": _now(),
            "history": [*(request.get("history") or []), _history(
                PAY_REQUESTED, actor,
                f"{payload.description} of {mailer.money_label(payload.amount, currency)} requested"
                + (" through Razorpay" if use_gateway else ""),
            )],
        })

    merged["_notified"] = _send_payment_request(merged) if payload.notify_applicant else False
    return merged


def _send_payment_request(request: dict) -> bool:
    payment = request.get("payment") or {}
    to, name = _family(request)
    return _mail(
        mailer.send_payment_requested_email, to=to, contact_name=name,
        student_name=request.get("student_full_name") or "", reference=request.get("reference") or "",
        amount=payment.get("amount"), currency=payment.get("currency"),
        description=payment.get("description") or "Admission fee",
        portal_url=portal_url(request) or "", pay_url=payment.get("link_url"),
        due_date=_deadline_label(payment.get("due_date")),
        instructions=payment.get("instructions"), note=payment.get("note"),
    )


def _assert_payment_open(request: dict) -> dict:
    payment = request.get("payment")
    if request.get("status") not in (PAY_REQUESTED, PAY_SUBMITTED) or not payment:
        if request.get("status") in (PAY_VERIFIED, ADMITTED):
            raise HTTPException(status_code=409, detail="This payment has already been confirmed. Thank you.")
        raise HTTPException(status_code=409, detail="No payment is being collected at this stage.")
    return payment


async def submit_payment_proof(request: dict, *, transaction_id: str | None, paid_on: date | None,
                               amount: float | None, method: str | None, note: str | None,
                               file: UploadFile | None) -> dict:
    """[Family] A transaction id, a screenshot, or both, for the office to check."""
    _assert_payment_open(request)
    transaction_id = (transaction_id or "").strip()[:120] or None
    note = (note or "").strip()[:1000] or None
    method = (method or "").strip().upper()[:30] or None
    has_file = file is not None and bool(getattr(file, "filename", None))
    if not transaction_id and not has_file:
        raise HTTPException(
            status_code=400,
            detail="Enter the transaction ID, or attach a screenshot of the payment.",
        )

    # A gateway payment that did go through needs no proof: confirm it and say so.
    synced, changed = sync_gateway(request, force=True)
    if changed and synced.get("status") == PAY_VERIFIED:
        return synced

    files = []
    if has_file:
        data, name, content_type = await _read_upload(file)
        files.append(_store(request, "payment", data, name, content_type))

    submission = {
        "id": uuid.uuid4().hex[:12],
        "transaction_id": transaction_id,
        "paid_on": paid_on.isoformat() if paid_on else None,
        "amount": float(amount) if amount else None,
        "method": method,
        "note": note,
        "files": files,
        "submitted_at": _now(),
        "status": "PENDING",
    }
    with _lock_for(request["id"]):
        request = _reload(request)
        payment = _assert_payment_open(request)
        payment = {**payment, "status": "SUBMITTED",
                   "submissions": [*(payment.get("submissions") or []), submission]}
        merged = _save(request, {
            "status": PAY_SUBMITTED,
            "payment": payment,
            "history": [*(request.get("history") or []), _history(
                PAY_SUBMITTED, None,
                "Payment details submitted" + (f" (transaction {transaction_id})" if transaction_id else ""),
            )],
        })

    to, name = _family(merged)
    _mail(mailer.send_payment_received_email, to=to, contact_name=name,
          student_name=merged.get("student_full_name") or "", reference=merged.get("reference") or "",
          amount=submission["amount"] or payment.get("amount"), currency=payment.get("currency"),
          transaction=transaction_id, confirmed=False, portal_url=portal_url(merged))
    _alert(merged, "Payment details have been submitted for verification.")
    return merged


def review_payment(request: dict, payload, actor) -> dict:
    """
    [Office] Confirm the money, or send the proof back. Confirming works without any proof
    too - the fee paid in cash at the counter.
    """
    status = request.get("status")
    payment = request.get("payment")
    if status not in (PAY_REQUESTED, PAY_SUBMITTED) or not payment:
        raise HTTPException(status_code=400, detail=_cannot(status, "review a payment"))
    if not payload.approve and status != PAY_SUBMITTED:
        raise HTTPException(status_code=400, detail="There are no payment details from the family to reject.")
    if not payload.approve and not payload.note:
        raise HTTPException(status_code=400, detail="Tell the family what did not match, so they can correct it.")

    with _lock_for(request["id"]):
        request = _reload(request)
        payment = dict(request.get("payment") or {})
        submissions = [dict(s) for s in payment.get("submissions") or []]
        pending = next((s for s in reversed(submissions) if s.get("status") == "PENDING"), None)
        if pending is not None:
            pending["status"] = "ACCEPTED" if payload.approve else "REJECTED"
            pending["review_note"] = payload.note
        payment["submissions"] = submissions
        payment["review_note"] = payload.note
        if payload.approve:
            payment.update({
                "status": "VERIFIED", "verified_at": _now(), "verified_by_name": actor.full_name,
                "verified_via": "OFFICE",
                "amount_paid": (pending or {}).get("amount") or payment.get("amount"),
            })
            new_status, note = PAY_VERIFIED, payload.note or "Payment confirmed"
        else:
            payment["status"] = "REJECTED"
            new_status, note = PAY_REQUESTED, f"Payment details not confirmed: {payload.note}"
        merged = _save(request, {
            "status": new_status, "payment": payment,
            "reviewed_by": int(actor.id), "reviewed_by_name": actor.full_name, "reviewed_at": _now(),
            "history": [*(request.get("history") or []), _history(new_status, actor, note)],
        })

    notified = False
    if payload.notify_applicant:
        to, name = _family(merged)
        if payload.approve:
            notified = _mail(
                mailer.send_payment_received_email, to=to, contact_name=name,
                student_name=merged.get("student_full_name") or "", reference=merged.get("reference") or "",
                amount=payment.get("amount_paid"), currency=payment.get("currency"),
                transaction=(pending or {}).get("transaction_id"), confirmed=True,
                portal_url=portal_url(merged),
            )
        else:
            notified = _mail(
                mailer.send_payment_issue_email, to=to, contact_name=name,
                student_name=merged.get("student_full_name") or "", reference=merged.get("reference") or "",
                portal_url=portal_url(merged) or "", note=payload.note,
            )
    merged["_notified"] = notified
    return merged


# How often the family's page may make us ask Razorpay: a page load at most every ten
# seconds, an explicit "check" press every three. Every load would otherwise be a call to a
# third party, and an impatient double-click two.
PASSIVE_SYNC_SECONDS = 10.0
EXPLICIT_SYNC_SECONDS = 3.0
_last_sync: dict[str, float] = {}


def sync_gateway(request: dict, force: bool = False,
                 min_interval: float = PASSIVE_SYNC_SECONDS) -> tuple[dict, bool]:
    """
    Read a Razorpay link back and confirm the payment when it was paid. Returns the request
    and whether anything changed. Quietly does nothing for a manual payment, without keys, or
    when Razorpay cannot be reached - the family can still send proof.
    """
    payment = request.get("payment") or {}
    if (payment.get("provider") != "RAZORPAY" or not payment.get("gateway_link_id")
            or payment.get("status") == "VERIFIED" or not razorpay.is_configured()
            or request.get("status") not in (PAY_REQUESTED, PAY_SUBMITTED)):
        return request, False
    key = str(request["id"])
    now = time.monotonic()
    if not force and now - _last_sync.get(key, 0.0) < min_interval:
        return request, False
    _last_sync[key] = now

    try:
        link = razorpay.fetch_payment_link(payment["gateway_link_id"])
    except razorpay.RazorpayError as exc:
        logger.warning("Could not read payment link for %s: %s", request.get("reference"), exc)
        return request, False
    return _apply_gateway_link(request, link)


def _apply_gateway_link(request: dict, link: dict) -> tuple[dict, bool]:
    gateway_status = str(link.get("status") or "")
    with _lock_for(request["id"]):
        request = _reload(request)
        payment = dict(request.get("payment") or {})
        if payment.get("gateway_link_id") != link.get("id"):
            return request, False  # an older link; the fee was re-issued since
        if gateway_status != "paid":
            if payment.get("gateway_status") == gateway_status:
                return request, False
            payment["gateway_status"] = gateway_status
            return _save(request, {"payment": payment}), True
        if payment.get("status") == "VERIFIED":
            return request, False

        payments = link.get("payments") or []
        payment_id = (payments[-1] or {}).get("payment_id") if payments else None
        payment.update({
            "status": "VERIFIED", "gateway_status": gateway_status, "verified_at": _now(),
            "verified_by_name": "Razorpay", "verified_via": "GATEWAY",
            "gateway_payment_id": payment_id,
            "amount_paid": razorpay.from_minor(link.get("amount_paid") or link.get("amount"),
                                               payment.get("currency") or "INR"),
        })
        merged = _save(request, {
            "status": PAY_VERIFIED, "payment": payment,
            "history": [*(request.get("history") or []), {
                **_history(PAY_VERIFIED, None, f"Paid online (Razorpay {payment_id or link.get('id')})"),
                "by_name": "Razorpay",
            }],
        })

    to, name = _family(merged)
    _mail(mailer.send_payment_received_email, to=to, contact_name=name,
          student_name=merged.get("student_full_name") or "", reference=merged.get("reference") or "",
          amount=payment["amount_paid"], currency=payment.get("currency"),
          transaction=payment_id, confirmed=True, portal_url=portal_url(merged))
    _alert(merged, "The admission fee has been paid online.")
    return merged, True


def handle_razorpay_webhook(body: bytes, signature: str | None) -> dict:
    """The `payment_link.paid` event, trusted only with a valid signature."""
    import json

    if not razorpay.verify_webhook(body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")
    try:
        event = json.loads(body.decode("utf-8"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid body") from exc
    if event.get("event") != "payment_link.paid":
        return {"ok": True, "ignored": event.get("event")}
    link = ((event.get("payload") or {}).get("payment_link") or {}).get("entity") or {}
    request_id = (link.get("notes") or {}).get("request_id")
    if not request_id:
        return {"ok": True, "ignored": "no request id"}
    request = firestore_admission_requests.get_document(str(request_id))
    if not request:
        return {"ok": True, "ignored": "unknown request"}
    request["id"] = request.get("id") or int(request_id)
    payment_entity = ((event.get("payload") or {}).get("payment") or {}).get("entity") or {}
    if payment_entity.get("id") and not link.get("payments"):
        link["payments"] = [{"payment_id": payment_entity["id"]}]
    _apply_gateway_link(request, link)
    return {"ok": True}


# ---------------------------------------------------------------------------------------
# Resending the current step's email
# ---------------------------------------------------------------------------------------

def resend(request: dict) -> dict:
    request = ensure_token(request)
    status = request.get("status") or NEW
    if status == DOCS_REQUESTED:
        sent = _send_documents_request(request)
    elif status in (PAY_REQUESTED,):
        sent = _send_payment_request(request)
    elif status in (NEW, UNDER_REVIEW):
        to, name = _family(request)
        sent = _mail(mailer.send_admission_request_received_email, to=to, contact_name=name,
                     student_name=request.get("student_full_name") or "",
                     reference=request.get("reference") or "", class_name=request.get("class_name"),
                     year_name=request.get("academic_year_name"), portal_url=portal_url(request))
    elif status == ADMITTED:
        raise HTTPException(
            status_code=400,
            detail="The welcome email carried passwords that are not stored. Use Generate "
                   "credentials on the student and parent records to send new ones.",
        )
    else:
        raise HTTPException(status_code=400, detail="There is no email to resend at this stage.")
    return {
        "sent": sent,
        "detail": f"Email sent to {request.get('contact_email')}." if sent
        else "The email could not be sent. Check the mail settings, or share the portal link directly.",
    }


def _cannot(status: str | None, action: str) -> str:
    label = (status or NEW).replace("_", " ").lower()
    return f"You cannot {action} while the application is {label}."


# ---------------------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------------------

def admin_fields(request: dict) -> dict:
    """The pipeline part of the office's view, files signed for an hour."""
    payment = request.get("payment")
    if payment:
        payment = {
            **payment,
            "submissions": [
                {**s, "files": [_file_view(f, sign=True) for f in s.get("files") or []]}
                for s in payment.get("submissions") or []
            ],
        }
    return {
        "portal_url": portal_url(request),
        "documents": [
            {**doc, "files": [_file_view(f, sign=True) for f in doc.get("files") or []]}
            for doc in request.get("documents") or []
        ],
        "documents_note": request.get("documents_note"),
        "documents_deadline": request.get("documents_deadline"),
        "documents_requested_at": request.get("documents_requested_at"),
        "documents_submitted_at": request.get("documents_submitted_at"),
        "documents_verified_at": request.get("documents_verified_at"),
        "payment": payment,
    }


_STAGES = {
    NEW: ("Application received",
          "Thank you for applying. Our admissions team is reviewing the application and will "
          "email you with the next steps."),
    UNDER_REVIEW: ("Application under review",
                   "Our admissions team is reviewing the application and will email you with "
                   "the next steps."),
    WAITLISTED: ("On the waiting list",
                 "All seats are filled at the moment. The application is on our waiting list "
                 "and we will contact you as soon as a seat becomes available."),
    DOCS_REQUESTED: ("Upload documents",
                     "The application has been shortlisted. Please upload the documents below, "
                     "then press Submit for verification."),
    DOCS_SUBMITTED: ("Documents under verification",
                     "Thank you. Our team is verifying the documents and will get back to you "
                     "shortly."),
    DOCS_VERIFIED: ("Documents verified",
                    "All documents have been verified. We will send you the admission fee "
                    "details shortly."),
    PAY_REQUESTED: ("Pay the admission fee",
                    "Please pay the admission fee using the details below. If you have already "
                    "paid, share the transaction ID or a screenshot so we can confirm it."),
    PAY_SUBMITTED: ("Payment under verification",
                    "Thank you. Our accounts team is verifying the payment and will confirm it "
                    "by email."),
    PAY_VERIFIED: ("Payment confirmed",
                   "The payment has been confirmed. We are completing the admission and will "
                   "email you the admission details and login details shortly."),
    ADMITTED: ("Admission confirmed",
               "Welcome! The admission is complete. The login details for our learning app "
               "were sent to your email."),
    REJECTED: ("Application closed",
               "We are unable to offer a place at this time. Please contact the school office "
               "if you have any questions."),
}

_STEPS = [("application", "Application"), ("documents", "Documents"),
          ("payment", "Fee payment"), ("admission", "Admission")]
_STEP_OF = {
    NEW: 0, UNDER_REVIEW: 0, WAITLISTED: 0,
    DOCS_REQUESTED: 1, DOCS_SUBMITTED: 1,
    DOCS_VERIFIED: 2, PAY_REQUESTED: 2, PAY_SUBMITTED: 2,
    PAY_VERIFIED: 3, ADMITTED: 4,
}


def _steps(request: dict) -> list[dict]:
    status = request.get("status") or NEW
    if status == REJECTED:
        reached = 2 if request.get("payment") else 1 if request.get("documents") else 0
        return [{"key": k, "label": label,
                 "state": "done" if i < reached else "stopped" if i == reached else "upcoming"}
                for i, (k, label) in enumerate(_STEPS)]
    current = _STEP_OF.get(status, 0)
    return [{"key": k, "label": label,
             "state": "done" if i < current else "current" if i == current else "upcoming"}
            for i, (k, label) in enumerate(_STEPS)]


def portal_view(request: dict) -> dict:
    """What the family's page shows. Deliberately narrow: see `AdmissionPortalOut`."""
    from app.core.mailer import class_label

    status = request.get("status") or NEW
    title, message = _STAGES.get(status, _STAGES[NEW])
    if status == DOCS_REQUESTED and any(d.get("status") == "REJECTED" for d in request.get("documents") or []):
        title = "Some documents need your attention"
        message = ("Our team has asked for some documents to be uploaded again - the reasons are "
                   "shown below. Please upload them and press Submit for verification.")
    payment = request.get("payment")
    if status == PAY_REQUESTED and payment and payment.get("status") == "REJECTED":
        title = "We could not confirm the payment"
        message = ("Our accounts team could not match the payment details you sent. Please check "
                   "them and submit again, or contact the school office.")

    documents = [{
        "key": doc["key"], "label": doc["label"], "description": doc.get("description"),
        "required": doc.get("required", True), "status": doc.get("status") or "PENDING",
        "review_note": doc.get("review_note"),
        "files": [{k: f.get(k) for k in ("id", "name", "size", "uploaded_at")} for f in doc.get("files") or []],
    } for doc in request.get("documents") or []]

    portal_payment = None
    if payment:
        last = (payment.get("submissions") or [None])[-1]
        portal_payment = {
            **{k: payment.get(k) for k in ("amount", "currency", "description", "due_date",
                                           "link_url", "instructions", "provider", "status",
                                           "review_note")},
            "last_submission": {
                "transaction_id": last.get("transaction_id"), "paid_on": last.get("paid_on"),
                "amount": last.get("amount"), "submitted_at": last.get("submitted_at"),
                "status": last.get("status") or "PENDING", "has_attachment": bool(last.get("files")),
            } if last else None,
        }
        if status in (PAY_VERIFIED, ADMITTED):
            portal_payment["link_url"] = None  # nothing left to pay

    admitted = None
    if status == ADMITTED:
        admitted = {
            "class_name": class_label(request.get("admitted_class_name") or request.get("class_name")),
            "academic_year_name": request.get("admitted_year_name") or request.get("academic_year_name"),
            "admission_number": request.get("admission_number"),
            "login_email": request.get("admitted_student_email"),
        }

    profile = mailer.school_profile()
    required_ready = all(
        d["status"] in ("UPLOADED", "ACCEPTED") for d in documents if d["required"]
    )
    return {
        "reference": request.get("reference") or f"ADR-{request['id']}",
        "student_full_name": request.get("student_full_name") or "",
        "class_name": class_label(request.get("class_name")),
        "academic_year_name": request.get("academic_year_name"),
        "status": status,
        "stage_title": title,
        "stage_message": message,
        "steps": _steps(request),
        "submitted_at": request.get("submitted_at"),
        "documents": documents,
        "documents_note": request.get("documents_note"),
        "documents_deadline": request.get("documents_deadline"),
        "can_upload_documents": status == DOCS_REQUESTED,
        "can_submit_documents": status == DOCS_REQUESTED and required_ready
        and any(d["status"] == "UPLOADED" for d in documents),
        "payment": portal_payment,
        "can_submit_payment": status in (PAY_REQUESTED, PAY_SUBMITTED) and payment is not None,
        "admitted": admitted,
        "school": {
            "name": profile["school_name"],
            "contact_email": profile.get("school_contact_email") or None,
            "contact_phone": profile.get("school_contact_phone") or None,
            "website_url": profile.get("school_website_url") or None,
            "web_app_url": profile.get("web_app_url") or settings.LMS_LOGIN_URL or None,
            "mobile_app_url": profile.get("mobile_app_url") or None,
        },
        "max_file_mb": int(settings.ADMISSION_DOCUMENT_MAX_MB),
        "max_files_per_document": int(settings.ADMISSION_DOCUMENT_MAX_FILES),
        "accepted_types": ACCEPTED_TYPES,
    }


def pipeline_config(program: str = Program.LMS.value) -> dict:
    profile = mailer.school_profile()
    return {
        "gateway_available": razorpay.is_configured(),
        "gateway_provider": "RAZORPAY" if razorpay.is_configured() else None,
        "currency": school_currency(program),
        "portal_base_url": profile.get("admission_portal_url") or None,
        "email_configured": mailer.is_configured(),
    }
