"""
Creating user accounts, shared by the school LMS and the online tuition programme.

Both products create people, and they create them the same way: derive a login address,
check the identifiers are free, make the Firebase Auth account, write the profile, and stamp
the role claims. What differs is only *which products the new account may reach* - the
`programs` list - so that is the one parameter this takes, and everything else is one
implementation rather than two that drift apart.

Extracted here rather than left in the admin router because the tuition module needs to
create its own students and teachers. The two user bases genuinely overlap only sometimes: a
tuition student may be a school pupil here, or may be somebody who has never attended the
school at all. Making the tuition admin borrow the LMS's create-user endpoint would have
forced every tuition account to look like a school account first, and the "grant tuition
access afterwards" step is a box somebody eventually forgets to tick.

The rollback in `create_account` is the part worth reading: the Firebase Auth account is
made *before* the Firestore profile, because it supplies the uid the profile stores. If the
profile write then fails, the auth account is deleted - otherwise the email is taken by a
login with no profile behind it, and a retry fails forever with "email already exists" while
the admin can see no such user anywhere.
"""

import logging
from datetime import date, datetime
from enum import Enum

from fastapi import HTTPException

from app.core.config import settings
from app.core.credentials import generate_email
from app.core.enums import Program, UserRole, normalize_programs
from app.core.firebase import firestore_users
from app.core.firebase_auth import create_auth_user, delete_auth_user, set_role_claims
from app.schemas.user import UserProfileFields

logger = logging.getLogger("accounts")


def email_is_taken(email: str) -> bool:
    """Collision check for generated addresses, across every product's profiles."""
    return firestore_users.get_document_by_field("email", email) is not None


# School-issued identifiers that must not repeat. Both are optional, but a duplicate
# admission number is the kind of error that surfaces months later as two students sharing a
# report card, so it is rejected at write time.
UNIQUE_USER_FIELDS = (
    ("admission_number", "admission number"),
    ("employee_id", "employee ID"),
)


def assert_identifiers_free(stored: dict, exclude_user_id: int | None = None) -> None:
    """Rejects an admission number or employee ID already held by a different user."""
    for field, label in UNIQUE_USER_FIELDS:
        value = stored.get(field)
        if not value:
            continue

        clash = firestore_users.get_document_by_field(field, value)
        if clash and int(clash["id"]) != (exclude_user_id or -1):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"The {label} '{value}' is already assigned to "
                    f"{clash.get('full_name') or 'another user'} (id {clash['id']})."
                ),
            )


def profile_fields(payload: UserProfileFields, exclude_unset: bool = True) -> dict:
    """
    The profile portion of a create/update payload, as a Firestore-ready dict.

    Dates become ISO strings and enums their values, because Firestore has no native date
    type here and the rest of the codebase already stores ISO strings.
    """
    known = set(UserProfileFields.model_fields)
    raw = payload.model_dump(exclude_unset=exclude_unset, exclude_none=True)

    stored: dict = {}
    for key, value in raw.items():
        if key not in known:
            continue
        if isinstance(value, Enum):
            stored[key] = value.value
        elif isinstance(value, (date, datetime)):
            stored[key] = value.isoformat()
        else:
            stored[key] = value
    return stored


def resolve_email(payload) -> str:
    """
    The login address for a new account: the one supplied, or one derived from the name.

    Derivation is firstname.lastname@USER_EMAIL_DOMAIN with a numeric suffix on collision, so
    an administrator onboarding thirty students by name does not have to invent thirty
    addresses.
    """
    if payload.email:
        email = payload.email.strip().lower()
        if email_is_taken(email):
            raise HTTPException(status_code=400, detail="User with this email already exists")
        return email

    if not payload.full_name.strip():
        raise HTTPException(
            status_code=400, detail="full_name is required to generate an email address"
        )
    try:
        return generate_email(
            payload.full_name, settings.resolved_user_email_domain, is_taken=email_is_taken
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def create_account(payload, role: UserRole | None = None,
                   programs: list[str] | None = None) -> dict:
    """
    Creates one user: Firebase Auth account, Firestore profile, and role claims.

    `role` and `programs` override whatever the payload carries. That is how the tuition
    endpoints guarantee what they create - a route called "add a tuition student" must not be
    able to mint an administrator because the request body said so, and forcing both here
    means the guarantee lives in one place rather than in each caller's validation.

    Returns the stored profile, including its `id`.
    """
    email = resolve_email(payload)
    effective_role = role or getattr(payload, "role", UserRole.STUDENT)
    # The tuition payloads carry no `programs` field at all - their routes decide it - so this
    # reads defensively rather than assuming the LMS schema's shape.
    if programs is None:
        declared = getattr(payload, "programs", None) or []
        programs = [getattr(p, "value", p) for p in declared]
    effective_programs = normalize_programs(programs)

    profile = profile_fields(payload)
    assert_identifiers_free(profile)

    firebase_uid = create_auth_user(email, payload.password, payload.full_name)

    user_id = firestore_users.get_next_numeric_id()
    document = {
        **profile,
        "full_name": payload.full_name,
        "email": email,
        "firebase_uid": firebase_uid,
        "role": effective_role.value,
        "programs": effective_programs,
        "is_active": True,
        # Reminders are opt-out: enrolling somebody means intending to tell them when class
        # starts, so the default has to be on unless the admin says otherwise.
        "reminder_opt_in": profile.get("reminder_opt_in", True),
        "created_at": datetime.utcnow().isoformat(),
    }

    try:
        firestore_users.add_document(str(user_id), document)
    except Exception:
        # See the module docstring: an auth account with no profile behind it takes the
        # email address hostage and makes every retry fail.
        if firebase_uid:
            delete_auth_user(firebase_uid)
        raise

    if firebase_uid:
        set_role_claims(firebase_uid, effective_role, user_id)

    document["id"] = user_id
    logger.info(
        "Created %s account %s (%s) with programme access %s.",
        effective_role.value, user_id, email, effective_programs,
    )
    return document


def next_identifier(field: str, prefix: str) -> str:
    """
    A free admission number or employee id, of the form PREFIX-YEAR-0001.

    The brief asks that every tuition student carry a unique id, and asking an administrator
    to invent one per student is how duplicates and blanks get into the data. So one is
    issued when none is supplied.

    The sequence is derived from the ids already in use for this year's prefix rather than
    from a stored counter: a counter is one more document to keep in step, and this reads a
    collection that is small and already in memory for most of these requests. The result is
    then checked against the whole user table, and the loop steps past anything taken -
    including an id an administrator typed in by hand, which no counter would know about.
    """
    year = datetime.utcnow().year
    stem = f"{prefix}-{year}-"

    highest = 0
    for user in firestore_users.list_all():
        value = str(user.get(field) or "")
        if value.startswith(stem):
            tail = value[len(stem):]
            if tail.isdigit():
                highest = max(highest, int(tail))

    # Bounded rather than `while True`: a bug that made every candidate look taken would
    # otherwise hang the request thread instead of failing with something readable.
    for offset in range(1, 1000):
        candidate = f"{stem}{highest + offset:04d}"
        if firestore_users.get_document_by_field(field, candidate) is None:
            return candidate

    raise HTTPException(
        status_code=500,
        detail=f"Could not allocate a free {field} after 1000 attempts. Supply one explicitly.",
    )


def create_tuition_account(payload, role: UserRole) -> dict:
    """
    Creates a tuition account, issuing an identifier when none was supplied.

    Tuition access is always granted; LMS access only when the caller asked for it with
    `also_lms`. The default is deliberate: somebody added through the tuition module is a
    tuition user, and an account that quietly also reached the school's classes and marks
    would be a privacy problem rather than a convenience.
    """
    programs = [Program.TUITION.value]
    if getattr(payload, "also_lms", False):
        programs.append(Program.LMS.value)

    if role == UserRole.STUDENT and not payload.admission_number:
        payload.admission_number = next_identifier(
            "admission_number", settings.TUITION_ADMISSION_PREFIX
        )
    if role == UserRole.TEACHER and not getattr(payload, "employee_id", None):
        payload.employee_id = next_identifier(
            "employee_id", settings.TUITION_EMPLOYEE_PREFIX
        )

    account = create_account(payload, role=role, programs=programs)

    # Subjects a teacher can take. Stored on the profile rather than as mapping rows: it is a
    # hint for the enrollment screen's candidate list, not an assignment - the assignment is
    # the enrollment itself, and duplicating it as rows would give two answers to "who
    # teaches this?".
    subject_ids = getattr(payload, "subject_ids", None)
    if subject_ids:
        firestore_users.add_document(str(account["id"]), {"subject_ids": list(subject_ids)})
        account["subject_ids"] = list(subject_ids)

    return account
