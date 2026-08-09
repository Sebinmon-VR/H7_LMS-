"""
Startup bootstrap.

Because there is no public registration endpoint, the system needs exactly one account to
exist before anyone can sign in and create the rest. This module ensures that single
administrator exists and does nothing else.

No demo classes, subjects, teachers, or students are created. Everything beyond the
bootstrap admin is entered through the API by a signed-in administrator, so the database
starts genuinely empty. Set SEED_DEMO_DATA=True only if you want throwaway sample records
for local experimentation.
"""

import logging
from datetime import datetime

from app.core.config import settings
from app.core.enums import UserRole
from app.core.firebase import (
    firestore_classes,
    firestore_subjects,
    firestore_users,
    generate_id,
)
from app.core.firebase_auth import (
    create_auth_user,
    get_auth_user_by_email,
    is_available,
    set_role_claims,
)

logger = logging.getLogger("init_db")


def ensure_bootstrap_admin() -> dict | None:
    """
    Creates the bootstrap administrator if it does not already exist.

    Idempotent and non-destructive: an existing profile is never overwritten, and an
    existing Firebase account is linked rather than replaced. Safe to run on every startup.
    """
    email = settings.BOOTSTRAP_ADMIN_EMAIL
    if not email:
        logger.warning("BOOTSTRAP_ADMIN_EMAIL is not set; skipping admin bootstrap.")
        return None

    existing = firestore_users.get_document_by_field("email", email)
    if existing:
        # Repair a profile whose Firebase link is missing, which otherwise blocks sign-in.
        if not existing.get("firebase_uid") and is_available():
            record = get_auth_user_by_email(email)
            uid = record.uid if record else create_auth_user(
                email, settings.BOOTSTRAP_ADMIN_PASSWORD, settings.BOOTSTRAP_ADMIN_NAME
            )
            if uid:
                firestore_users.add_document(str(existing["id"]), {"firebase_uid": uid})
                set_role_claims(uid, UserRole.ADMIN.value, int(existing["id"]))
                logger.info("Linked bootstrap admin '%s' to Firebase uid %s.", email, uid)
        return existing

    if not is_available():
        logger.warning(
            "Firebase Auth unavailable; cannot bootstrap admin '%s'. "
            "Run scripts/provision_auth_user.py once credentials are configured.", email
        )
        return None

    firebase_uid = create_auth_user(
        email, settings.BOOTSTRAP_ADMIN_PASSWORD, settings.BOOTSTRAP_ADMIN_NAME
    )
    if not firebase_uid:
        logger.error("Could not create the Firebase account for bootstrap admin '%s'.", email)
        return None

    user_id = generate_id()
    admin = {
        "full_name": settings.BOOTSTRAP_ADMIN_NAME,
        "email": email,
        "firebase_uid": firebase_uid,
        "role": UserRole.ADMIN.value,
        "is_active": True,
        "created_at": datetime.utcnow().isoformat(),
    }
    firestore_users.add_document(str(user_id), admin)
    set_role_claims(firebase_uid, UserRole.ADMIN.value, user_id)

    admin["id"] = user_id
    logger.info("Created bootstrap administrator: %s", email)
    return admin


def _seed_demo_data() -> None:
    """Creates a couple of sample records. Opt-in via SEED_DEMO_DATA, off by default."""
    if not any(c.get("code") == "CLASS-10A" for c in firestore_classes.list_all()):
        class_id = generate_id()
        firestore_classes.add_document(str(class_id), {
            "name": "Grade 10 - Section A",
            "code": "CLASS-10A",
            "description": "Sample class",
            "created_at": datetime.utcnow().isoformat(),
        })

    if not any(s.get("code") == "MATH101" for s in firestore_subjects.list_all()):
        subject_id = generate_id()
        firestore_subjects.add_document(str(subject_id), {
            "name": "Mathematics",
            "code": "MATH101",
            "description": "Sample subject",
            "created_at": datetime.utcnow().isoformat(),
        })

    logger.info("Demo data seeded.")


def init_db() -> None:
    """Ensures the bootstrap administrator exists. Called from the application lifespan."""
    ensure_bootstrap_admin()

    if settings.SEED_DEMO_DATA:
        _seed_demo_data()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
