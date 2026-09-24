"""
Online admission requests: the website form on one side, the office's queue on the other.

A request is not a student. The public endpoint writes a document and nothing else - no
login, no roster entry, no invoice - so the worst a stranger with the form's URL can do is
fill the queue, and the per-address limit below bounds even that. Everything that turns a
request into a person happens in `admit`, behind the admin guard, and reuses the same account,
enrollment and family code the Users screen does. Two ways of creating a student would drift;
this module deliberately has none of its own.

Three things here are worth knowing before changing them:

* **The class and the year are snapshotted at submission** (`class_name`,
  `academic_year_name`). A class renamed in July must not blank an April application, and
  the admit step re-resolves the live records anyway.
* **`admit` validates everything before it writes anything**, then writes in the order
  student, enrollment, family, parent, credentials. A failure after the student exists is
  reported as a warning on a request that is still marked ADMITTED, because the student is
  real by then and marking the request NEW again would invite a second student.
* **Emails never fail a request.** Acknowledgements, office alerts and decision letters are
  best-effort through the mailer, which returns False rather than raising; the flags on the
  response say whether they went.
"""

import logging
import threading
import time
from datetime import date, datetime

from fastapi import HTTPException

from app.core import mailer
from app.core.config import settings
from app.core.enums import (
    OPEN_ADMISSION_REQUEST_VALUES, AcademicYearStatus, AdmissionRequestStatus, Gender,
    GuardianRelation, Program, UserRole,
)
from app.core.firebase import (
    firestore_admission_requests, firestore_classes, firestore_student_enrollments,
    firestore_users, require_document,
)
from app.schemas.family import ParentCreate, ParentLinkCreate
from app.schemas.user import UserCreate
from app.services import accounts as account_service
from app.services import admissions as year_service
from app.services import families as family_service

logger = logging.getLogger("admission_requests")

NEW = AdmissionRequestStatus.NEW.value
UNDER_REVIEW = AdmissionRequestStatus.UNDER_REVIEW.value
WAITLISTED = AdmissionRequestStatus.WAITLISTED.value
ADMITTED = AdmissionRequestStatus.ADMITTED.value
REJECTED = AdmissionRequestStatus.REJECTED.value

# Decisions the family is written to about. UNDER_REVIEW is an internal state.
NOTIFIABLE_DECISIONS = frozenset({WAITLISTED, REJECTED, ADMITTED})

RATE_WINDOW_SECONDS = 3600.0


def _now() -> str:
    return datetime.utcnow().isoformat()


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _enum_value(value):
    return getattr(value, "value", value)


def _clean(value) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return value


# ---------------------------------------------------------------------------------------
# The per-address limit
#
# In memory and per process, which is enough: the point is to stop one script filing a
# thousand applications in a minute, not to account precisely across workers. A restart
# forgets the counts, and that is fine.
# ---------------------------------------------------------------------------------------

_rate_lock = threading.Lock()
_recent_by_client: dict[str, list[float]] = {}


def assert_rate_limit(client_key: str | None) -> None:
    limit = int(settings.ADMISSION_REQUEST_RATE_LIMIT or 0)
    if not client_key or limit <= 0:
        return

    now = time.monotonic()
    with _rate_lock:
        recent = [t for t in _recent_by_client.get(client_key, []) if now - t < RATE_WINDOW_SECONDS]
        if len(recent) >= limit:
            raise HTTPException(
                status_code=429,
                detail="Too many admission requests have been sent from this connection. "
                       "Please try again in an hour, or contact the school office.",
                headers={"Retry-After": str(int(RATE_WINDOW_SECONDS))},
            )
        recent.append(now)
        _recent_by_client[client_key] = recent

        # Keep the table from growing forever on a busy public endpoint.
        if len(_recent_by_client) > 5000:
            for key in [k for k, v in _recent_by_client.items()
                        if not v or now - v[-1] >= RATE_WINDOW_SECONDS]:
                _recent_by_client.pop(key, None)


# ---------------------------------------------------------------------------------------
# What the form is offered
# ---------------------------------------------------------------------------------------

def open_years(program: str) -> list[dict]:
    """Session years currently taking admissions for a product, newest first."""
    return [
        y for y in year_service.list_years(program)
        if y.get("admissions_open", True)
        and (y.get("status") or AcademicYearStatus.UPCOMING.value) != AcademicYearStatus.CLOSED.value
    ]


def default_year(program: str) -> dict | None:
    """
    The year an application lands in when the family does not choose.

    The current year if it is still taking admissions; otherwise the earliest open year that
    has not ended - which in August is next year's intake, opened while this year is full.
    None when no year is open, or none exists.
    """
    opens = open_years(program)
    if not opens:
        return None

    current = year_service.current_year(program)
    if current and any(str(y["id"]) == str(current["id"]) for y in opens):
        return current

    today = date.today().isoformat()
    live = sorted(
        (y for y in opens if str(y.get("end_date") or "") >= today),
        key=lambda y: str(y.get("start_date") or ""),
    )
    return live[0] if live else opens[0]


def _year_view(year: dict | None) -> dict | None:
    if not year:
        return None
    return {
        "id": int(year["id"]),
        "name": year.get("name"),
        "start_date": year.get("start_date"),
        "end_date": year.get("end_date"),
        "is_current": bool(year.get("is_current")),
    }


def list_classes() -> list[dict]:
    """The classes on offer, in the order a prospectus lists them (KG before Class 10)."""
    rows = list(firestore_classes.list_all())
    rows.sort(key=lambda c: year_service._natural_key(c.get("name") or ""))
    return [{"id": int(c["id"]), "name": c.get("name"), "code": c.get("code")} for c in rows]


def options(program: str = Program.LMS.value) -> dict:
    years = year_service.list_years(program)
    opens = open_years(program)

    if not settings.ADMISSION_REQUESTS_ENABLED:
        accepting, reason = False, "Online admission requests are not being taken at the moment."
    elif years and not opens:
        accepting, reason = False, "Admissions are closed for now. Please contact the school office."
    else:
        # No years at all means a school that has not set admissions up yet; the form still
        # works and the request simply carries no session.
        accepting, reason = True, None

    return {
        "accepting": accepting,
        "closed_reason": reason,
        "academic_year": _year_view(default_year(program)),
        "academic_years": [_year_view(y) for y in opens],
        "classes": list_classes(),
        "relations": [r.value for r in GuardianRelation],
    }


# ---------------------------------------------------------------------------------------
# Submitting
# ---------------------------------------------------------------------------------------

def _parent_doc(parent) -> dict:
    return {
        "relation": _enum_value(parent.relation),
        "full_name": parent.full_name,
        "phone": parent.phone,
        "email": parent.email,
        "occupation": parent.occupation,
        "is_primary": bool(parent.is_primary),
    }


def _primary_parent(request: dict) -> dict:
    parents = request.get("parents") or []
    for parent in parents:
        if parent.get("is_primary"):
            return parent
    if parents:
        return parents[0]
    return {
        "relation": GuardianRelation.GUARDIAN.value,
        "full_name": request.get("contact_name"),
        "phone": request.get("contact_phone"),
        "email": request.get("contact_email"),
    }


def _next_reference(rows: list[dict]) -> str:
    """ADR-<year>-<sequence>, the sequence read from what already exists this year."""
    stem = f"ADR-{datetime.utcnow().year}-"
    highest = 0
    for row in rows:
        reference = str(row.get("reference") or "")
        if reference.startswith(stem) and reference[len(stem):].isdigit():
            highest = max(highest, int(reference[len(stem):]))
    return f"{stem}{highest + 1:04d}"


def _duplicate_in(rows: list[dict], name: str, birth: date, program: str) -> dict | None:
    """
    An open request for the same child.

    Name and date of birth together, case-insensitively: the family that pressed Submit
    twice, or filled the form in again a week later because they heard nothing.
    """
    wanted = (name.strip().lower(), birth.isoformat())
    for row in rows:
        if row.get("status") not in OPEN_ADMISSION_REQUEST_VALUES:
            continue
        if (row.get("program") or Program.LMS.value) != program:
            continue
        key = (str(row.get("student_full_name") or "").strip().lower(),
               str(row.get("date_of_birth") or "")[:10])
        if key == wanted:
            return row
    return None


def submit(payload, program: str = Program.LMS.value, client_key: str | None = None) -> dict:
    if not settings.ADMISSION_REQUESTS_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Online admission requests are not being taken at the moment.",
        )

    # A filled honeypot is a bot. Acknowledge and drop: a refusal teaches it what to change.
    if _clean(payload.website):
        logger.info("Dropped an admission request that filled the honeypot field.")
        return {
            "id": 0,
            "reference": f"ADR-{datetime.utcnow().year}-0000",
            "status": NEW,
            "student_full_name": payload.student_full_name,
            "class_name": payload.class_applied,
            "academic_year_name": None,
            "submitted_at": _now(),
            "acknowledgement_sent": False,
            "detail": "Thank you. Your admission request has been received.",
        }

    assert_rate_limit(client_key)

    years = year_service.list_years(program)
    opens = open_years(program)
    if years and not opens:
        raise HTTPException(
            status_code=409,
            detail="Admissions are closed for now. Please contact the school office.",
        )

    if payload.academic_year_id is not None:
        year = next((y for y in opens if int(y["id"]) == int(payload.academic_year_id)), None)
        if year is None:
            raise HTTPException(
                status_code=400, detail="That session is not taking admissions. Pick another."
            )
    else:
        year = default_year(program)

    class_room = None
    if payload.class_id is not None:
        class_room = firestore_classes.get_document(str(payload.class_id))
        if not class_room:
            raise HTTPException(
                status_code=400, detail="That class is no longer offered. Pick another."
            )
    class_name = (class_room or {}).get("name") or payload.class_applied

    rows = firestore_admission_requests.list_all()
    duplicate = _duplicate_in(rows, payload.student_full_name, payload.date_of_birth, program)
    if duplicate:
        raise HTTPException(
            status_code=409,
            detail=(
                f"We already have an application for {payload.student_full_name} "
                f"(reference {duplicate.get('reference')}). The office will be in touch; "
                f"please quote that reference if you need to contact us."
            ),
        )

    parents = [_parent_doc(p) for p in payload.parents]
    if not any(p["is_primary"] for p in parents):
        parents[0]["is_primary"] = True
    primary = next(p for p in parents if p["is_primary"])

    now = _now()
    request_id = firestore_admission_requests.get_next_numeric_id()
    document = {
        "reference": _next_reference(rows),
        "program": program,
        "status": NEW,

        "student_full_name": payload.student_full_name,
        "date_of_birth": _iso(payload.date_of_birth),
        "gender": _enum_value(payload.gender),
        "blood_group": payload.blood_group,
        "nationality": payload.nationality,
        "previous_school": payload.previous_school,
        "previous_class": payload.previous_class,

        "class_id": int(class_room["id"]) if class_room else None,
        "class_name": class_name,
        "academic_year_id": int(year["id"]) if year else None,
        "academic_year_name": (year or {}).get("name"),
        "syllabus": payload.syllabus,
        "medium": payload.medium,

        "parents": parents,
        "contact_name": primary["full_name"],
        "contact_phone": primary["phone"],
        "contact_email": primary["email"],

        "address_line1": payload.address_line1,
        "address_line2": payload.address_line2,
        "city": payload.city,
        "state": payload.state,
        "postal_code": payload.postal_code,
        "country": payload.country,

        "sibling_name": payload.sibling_name,
        "transport_required": bool(payload.transport_required),
        "medical_notes": payload.medical_notes,
        "message": payload.message,
        "how_heard": payload.how_heard,

        "consent": True,
        "consent_at": now,
        "source": payload.source or "website",
        "submitted_at": now,
        "history": [{"status": NEW, "at": now, "by": None, "by_name": None,
                     "note": "Submitted from the website"}],
        "internal_notes": [],
    }
    firestore_admission_requests.add_document(str(request_id), document)
    document["id"] = request_id
    logger.info("Admission request %s (%s) filed for %s.",
                request_id, document["reference"], document["student_full_name"])

    acknowledged = _notify_received(document)
    _alert_office(document)

    return {
        **document,
        "acknowledgement_sent": acknowledged,
        "detail": (
            f"Thank you. Your admission request has been received and its reference is "
            f"{document['reference']}. The office will contact you at {primary['email']} "
            f"or {primary['phone']} once it has been reviewed."
        ),
    }


def _notify_received(request: dict) -> bool:
    to = request.get("contact_email")
    if not to or not mailer.is_configured():
        return False
    try:
        return mailer.send_admission_request_received_email(
            to=to,
            contact_name=request.get("contact_name") or "Parent",
            student_name=request.get("student_full_name") or "",
            reference=request.get("reference") or "",
            class_name=request.get("class_name"),
            year_name=request.get("academic_year_name"),
        )
    except Exception as exc:  # pragma: no cover - never fail the request for its email
        logger.warning("Acknowledgement email for %s not sent: %s", request.get("reference"), exc)
        return False


def _alert_office(request: dict) -> None:
    to = (settings.ADMISSION_REQUEST_NOTIFY_EMAIL or "").strip()
    if not to or not mailer.is_configured():
        return
    admin_url = None
    if settings.LMS_LOGIN_URL:
        admin_url = settings.LMS_LOGIN_URL.rstrip("/").rsplit("/login", 1)[0] + "/admin/admission-requests"
    try:
        mailer.send_admission_request_alert_email(to, request, admin_url)
    except Exception as exc:  # pragma: no cover
        logger.warning("Office alert for %s not sent: %s", request.get("reference"), exc)


def _notify_decision(request: dict, status: str, note: str | None) -> bool:
    to = request.get("contact_email")
    if not to or status not in NOTIFIABLE_DECISIONS or not mailer.is_configured():
        return False
    try:
        return mailer.send_admission_decision_email(
            to=to,
            contact_name=request.get("contact_name") or "Parent",
            student_name=request.get("student_full_name") or "",
            reference=request.get("reference") or "",
            status=status,
            note=note,
            class_name=request.get("admitted_class_name") or request.get("class_name"),
            year_name=request.get("admitted_year_name") or request.get("academic_year_name"),
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("Decision email for %s not sent: %s", request.get("reference"), exc)
        return False


# ---------------------------------------------------------------------------------------
# The office's queue
# ---------------------------------------------------------------------------------------

def require_request(request_id) -> dict:
    return require_document(firestore_admission_requests, request_id, "Admission request")


def list_requests(status: str | None = None, program: str | None = None,
                  class_id=None, academic_year_id=None) -> list[dict]:
    """Newest first. Status is a real query; the rest narrow the same rows."""
    if status:
        rows = firestore_admission_requests.query_documents("status", "==", str(status))
    else:
        rows = firestore_admission_requests.list_all()

    if program:
        wanted = str(program).upper()
        rows = [r for r in rows if (r.get("program") or Program.LMS.value) == wanted]
    if class_id is not None:
        rows = [r for r in rows if str(r.get("class_id")) == str(class_id)]
    if academic_year_id is not None:
        rows = [r for r in rows if str(r.get("academic_year_id")) == str(academic_year_id)]

    return sorted(rows, key=lambda r: str(r.get("submitted_at") or ""), reverse=True)


def summary(program: str | None = None) -> dict:
    rows = list_requests(program=program)
    by_status = {s.value: 0 for s in AdmissionRequestStatus}
    for row in rows:
        by_status[row.get("status") or NEW] = by_status.get(row.get("status") or NEW, 0) + 1
    return {
        "total": len(rows),
        "open": sum(1 for r in rows if r.get("status") in OPEN_ADMISSION_REQUEST_VALUES),
        "by_status": by_status,
    }


def _history_entry(status: str, actor, note: str | None) -> dict:
    return {
        "status": status,
        "at": _now(),
        "by": int(actor.id) if actor is not None else None,
        "by_name": getattr(actor, "full_name", None),
        "note": note,
    }


def set_status(request: dict, new_status: str, actor, note: str | None = None,
               notify: bool = False) -> dict:
    """
    Review, waitlist, decline or reopen. Never admit - that is `admit`, which creates things.

    An ADMITTED request is final: the student exists, and moving the request back would
    leave the account with nothing explaining it. Everything else may move freely, so a
    declined family can be reconsidered when a seat frees up.
    """
    new_status = _enum_value(new_status)
    current = request.get("status") or NEW

    if current == ADMITTED:
        raise HTTPException(
            status_code=400,
            detail=f"This request was already admitted (student id "
                   f"{request.get('admitted_student_id')}). It cannot be changed.",
        )
    if new_status == ADMITTED:
        raise HTTPException(status_code=400, detail="Use the admit action to admit a request.")
    if new_status == current:
        raise HTTPException(status_code=400, detail=f"The request is already {new_status}.")

    now = _now()
    updates = {
        "status": new_status,
        "updated_at": now,
        "reviewed_by": int(actor.id),
        "reviewed_by_name": actor.full_name,
        "reviewed_at": now,
        "history": [*(request.get("history") or []), _history_entry(new_status, actor, note)],
    }
    if new_status in (WAITLISTED, REJECTED):
        updates["decision_note"] = note
    firestore_admission_requests.add_document(str(request["id"]), updates)
    merged = {**request, **updates}

    if notify:
        merged["_notified"] = _notify_decision(merged, new_status, note)
    logger.info("Admission request %s moved %s -> %s by %s.",
                request.get("reference"), current, new_status, actor.id)
    return merged


def add_note(request: dict, body: str, actor) -> dict:
    note = {
        "author_id": int(actor.id),
        "author_name": actor.full_name,
        "body": body,
        "at": _now(),
    }
    updates = {
        "internal_notes": [*(request.get("internal_notes") or []), note],
        "updated_at": note["at"],
    }
    firestore_admission_requests.add_document(str(request["id"]), updates)
    return {**request, **updates}


def delete_request(request: dict) -> None:
    """
    Removes the request outright.

    Allowed in any state. An admitted request's student is a separate record and stays; the
    link from student to request is lost, which is the trade the admin makes by deleting.
    """
    firestore_admission_requests.delete_document(str(request["id"]))
    logger.info("Deleted admission request %s (%s).", request["id"], request.get("reference"))


# ---------------------------------------------------------------------------------------
# Admitting
# ---------------------------------------------------------------------------------------

def _enroll(student_id: int, class_id: int) -> int:
    """The same enrollment write the admin's Enrollments screen makes, duplicate-safe."""
    existing = [
        e for e in firestore_student_enrollments.query_documents("student_id", "==", int(student_id))
        if str(e.get("class_id")) == str(class_id)
    ]
    if existing:
        return int(existing[0]["id"])
    enrollment_id = firestore_student_enrollments.get_next_numeric_id()
    firestore_student_enrollments.add_document(str(enrollment_id), {
        "student_id": int(student_id),
        "class_id": int(class_id),
        "enrolled_at": _now(),
    })
    return enrollment_id


def _relation(value) -> GuardianRelation:
    try:
        return GuardianRelation(str(value or "").upper())
    except ValueError:
        return GuardianRelation.GUARDIAN


def _gender(value) -> Gender | None:
    try:
        return Gender(str(value).upper()) if value else None
    except ValueError:
        return None


def _student_payload(request: dict, payload, program: str, year_id: int | None) -> UserCreate:
    """The create-user body the Users screen would have sent, built from the request."""
    primary = _primary_parent(request)
    relation = _relation(primary.get("relation")).value.title()

    fields = {
        "full_name": request["student_full_name"],
        "email": payload.student_email,
        "password": None,
        "role": UserRole.STUDENT,
        "programs": [Program(program)],
        "phone": primary.get("phone"),
        "date_of_birth": year_service._as_date(request.get("date_of_birth")),
        "gender": _gender(request.get("gender")),
        "blood_group": request.get("blood_group"),
        "address_line1": request.get("address_line1"),
        "address_line2": request.get("address_line2"),
        "city": request.get("city"),
        "state": request.get("state"),
        "postal_code": request.get("postal_code"),
        "country": request.get("country"),
        "admission_number": payload.admission_number,
        "roll_number": payload.roll_number,
        "admission_date": date.today(),
        "guardian_name": primary.get("full_name"),
        "guardian_phone": primary.get("phone"),
        "guardian_email": primary.get("email"),
        "guardian_relation": relation,
        "academic_year_id": year_id,
        "admission_category_id": payload.admission_category_id,
        "syllabus": request.get("syllabus"),
        "medium": request.get("medium"),
        "notes": f"Admitted from online admission request {request.get('reference')}.",
    }
    # Only the fields that have a value are set, so `profile_fields` (exclude_unset) writes
    # exactly what the form knew and nothing it did not.
    return UserCreate(**{k: v for k, v in fields.items() if v is not None})


def _ensure_parent(primary: dict, email: str | None, student_id: int, program: str,
                   actor) -> tuple[dict, bool]:
    """
    A PARENT login for the primary contact, linked to the new student.

    Reuses an existing parent with that email - a second child from the same family - and
    refuses an address that belongs to a teacher or a student rather than quietly turning
    it into a parent.
    """
    if not email:
        raise HTTPException(
            status_code=400,
            detail="The primary contact has no email address, so no parent login was created.",
        )

    existing = firestore_users.get_document_by_field("email", email)
    created = False
    if existing:
        if existing.get("role") != UserRole.PARENT.value:
            raise HTTPException(
                status_code=400,
                detail=f"{email} already belongs to a {existing.get('role', 'user').lower()} "
                       f"account, so no parent login was created. Link a parent from Families.",
            )
        parent = existing
    else:
        parent_payload = ParentCreate(**{
            k: v for k, v in {
                "full_name": primary.get("full_name") or "Parent",
                "email": email,
                "phone": primary.get("phone"),
                "programs": [Program(program)],
                "links": [],
            }.items() if v is not None
        })
        parent = account_service.create_account(
            parent_payload, role=UserRole.PARENT, programs=[program]
        )
        created = True

    family_service.create_link(
        parent["id"],
        ParentLinkCreate(
            student_id=int(student_id),
            relation=_relation(primary.get("relation")),
            is_primary=True,
            may_view_fees=True,
        ),
        actor.id,
    )
    return parent, created


def admit(request: dict, payload, actor) -> dict:
    """
    Create the student from the request, enrol them, and record the decision.

    Everything is checked before the first write; see the module docstring for the order
    of the writes and why a late failure becomes a warning rather than a rollback.
    """
    if request.get("status") == ADMITTED:
        raise HTTPException(
            status_code=400,
            detail=f"This request was already admitted (student id "
                   f"{request.get('admitted_student_id')}).",
        )

    program = (request.get("program") or Program.LMS.value).upper()

    # The class. Required when the school has classes at all: a student nobody can find on a
    # roster is the bug this step exists to prevent.
    class_id = payload.class_id if payload.class_id is not None else request.get("class_id")
    class_room = require_document(firestore_classes, class_id, "Class") if class_id is not None else None
    if class_room is None and firestore_classes.list_all():
        raise HTTPException(
            status_code=400,
            detail="Pick the class to enrol the student in. The family applied for "
                   f"'{request.get('class_name') or 'an unlisted class'}'.",
        )

    # The session year. What was applied for, else the current year, else none.
    year_id = payload.academic_year_id or request.get("academic_year_id")
    if year_id is None:
        current = year_service.current_year(program)
        year_id = int(current["id"]) if current else None
    year = year_service.require_year(year_id) if year_id is not None else None
    year_service.assert_admission_fields(None, payload.admission_category_id)

    primary = _primary_parent(request)
    student_payload = _student_payload(request, payload, program, year_id)
    account_service.issue_school_identifier(student_payload, UserRole.STUDENT)

    # ---- writes start here ----
    student = account_service.create_account(student_payload)
    warnings: list[str] = []

    enrollment_id = None
    if class_room is not None:
        try:
            enrollment_id = _enroll(student["id"], int(class_room["id"]))
        except Exception as exc:
            logger.exception("Enrollment failed after admitting request %s", request.get("reference"))
            warnings.append(f"The student was created but not enrolled in {class_room.get('name')}: {exc}")

    try:
        family_service.auto_place(student["id"], actor.id)
    except Exception as exc:  # pragma: no cover - household placement is a convenience
        logger.warning("Family placement skipped for student %s: %s", student["id"], exc)

    parent, parent_created = None, False
    if payload.create_parent_account:
        parent_email = payload.parent_email or primary.get("email")
        try:
            parent, parent_created = _ensure_parent(primary, parent_email, student["id"], program, actor)
        except HTTPException as exc:
            warnings.append(str(exc.detail))
        except Exception as exc:
            logger.exception("Parent account failed for request %s", request.get("reference"))
            warnings.append(f"Parent login not created: {exc}")

    credentials: list[dict] = []
    if payload.send_credentials:
        deliver_to = primary.get("email") or student["email"]
        try:
            credentials.append(account_service.issue_credentials(student, deliver_to=deliver_to))
        except HTTPException as exc:
            warnings.append(f"Student login details not issued: {exc.detail}")
        if parent is not None and parent_created:
            try:
                credentials.append(account_service.issue_credentials(parent))
            except HTTPException as exc:
                warnings.append(f"Parent login details not issued: {exc.detail}")

    now = _now()
    updates = {
        "status": ADMITTED,
        "updated_at": now,
        "reviewed_by": int(actor.id),
        "reviewed_by_name": actor.full_name,
        "reviewed_at": now,
        "decision_note": payload.note,
        "admitted_student_id": int(student["id"]),
        "admitted_student_email": student.get("email"),
        "admitted_parent_id": int(parent["id"]) if parent else None,
        "admitted_class_id": int(class_room["id"]) if class_room else None,
        "admitted_class_name": (class_room or {}).get("name"),
        "admitted_year_id": int(year["id"]) if year else None,
        "admitted_year_name": (year or {}).get("name"),
        "enrollment_id": enrollment_id,
        "admission_number": student.get("admission_number"),
        "history": [*(request.get("history") or []), _history_entry(ADMITTED, actor, payload.note)],
    }
    firestore_admission_requests.add_document(str(request["id"]), updates)
    merged = {**request, **updates}

    notified = _notify_decision(merged, ADMITTED, payload.note) if payload.notify_applicant else False

    logger.info("Admission request %s admitted as student %s by %s.",
                request.get("reference"), student["id"], actor.id)

    parts = [f"{student['full_name']} admitted as {student['email']}"]
    if class_room is not None and enrollment_id is not None:
        parts.append(f"enrolled in {class_room.get('name')}")
    if parent is not None:
        parts.append("parent login created" if parent_created else "linked to the existing parent login")
    if credentials:
        sent = sum(1 for c in credentials if c.get("email_sent"))
        parts.append(f"{sent} of {len(credentials)} login emails sent")
    if notified:
        parts.append("family notified")

    return {
        "request": present(merged),
        "student": student,
        "parent": parent,
        "parent_created": parent_created,
        "enrollment_id": enrollment_id,
        "credentials": credentials,
        "warnings": warnings,
        "applicant_notified": notified,
        "detail": "; ".join(parts) + ".",
    }


# ---------------------------------------------------------------------------------------
# Presenting
# ---------------------------------------------------------------------------------------

def present(request: dict) -> dict:
    class_name = request.get("class_name")
    if request.get("class_id") is not None:
        live = firestore_classes.get_document(str(request["class_id"]))
        if live:
            class_name = live.get("name")

    return {
        "id": int(request["id"]),
        "reference": request.get("reference") or f"ADR-{request['id']}",
        "program": (request.get("program") or Program.LMS.value),
        "status": request.get("status") or NEW,

        "student_full_name": request.get("student_full_name") or "",
        "date_of_birth": request.get("date_of_birth"),
        "gender": request.get("gender"),
        "blood_group": request.get("blood_group"),
        "nationality": request.get("nationality"),
        "previous_school": request.get("previous_school"),
        "previous_class": request.get("previous_class"),

        "class_id": request.get("class_id"),
        "class_name": class_name,
        "academic_year_id": request.get("academic_year_id"),
        "academic_year_name": request.get("academic_year_name"),
        "syllabus": request.get("syllabus"),
        "medium": request.get("medium"),

        "parents": request.get("parents") or [],
        "contact_name": request.get("contact_name"),
        "contact_phone": request.get("contact_phone"),
        "contact_email": request.get("contact_email"),

        "address_line1": request.get("address_line1"),
        "address_line2": request.get("address_line2"),
        "city": request.get("city"),
        "state": request.get("state"),
        "postal_code": request.get("postal_code"),
        "country": request.get("country"),

        "sibling_name": request.get("sibling_name"),
        "transport_required": bool(request.get("transport_required")),
        "medical_notes": request.get("medical_notes"),
        "message": request.get("message"),
        "how_heard": request.get("how_heard"),
        "source": request.get("source"),

        "submitted_at": request.get("submitted_at"),
        "updated_at": request.get("updated_at"),
        "reviewed_by": request.get("reviewed_by"),
        "reviewed_by_name": request.get("reviewed_by_name"),
        "reviewed_at": request.get("reviewed_at"),
        "decision_note": request.get("decision_note"),

        "admitted_student_id": request.get("admitted_student_id"),
        "admitted_student_email": request.get("admitted_student_email"),
        "admitted_parent_id": request.get("admitted_parent_id"),
        "admitted_class_id": request.get("admitted_class_id"),
        "admitted_class_name": request.get("admitted_class_name"),
        "admitted_year_id": request.get("admitted_year_id"),
        "admitted_year_name": request.get("admitted_year_name"),
        "enrollment_id": request.get("enrollment_id"),
        "admission_number": request.get("admission_number"),

        "internal_notes": request.get("internal_notes") or [],
        "history": request.get("history") or [],
    }
