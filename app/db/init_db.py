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

from app.core import identity
from app.core.config import settings
from app.core.enums import UserRole
from app.core.firebase import (
    firestore_classes,
    firestore_subjects,
    firestore_users,
    generate_id,
)

logger = logging.getLogger("init_db")


def ensure_bootstrap_admin() -> dict | None:
    """
    Creates the bootstrap administrator if it does not already exist.

    Idempotent and non-destructive: an existing profile is never overwritten, and its
    password is never touched - BOOTSTRAP_ADMIN_PASSWORD is a default that ships in source,
    and writing it over a password somebody chose would reopen an account they had closed.
    Safe to run on every startup.
    """
    email = (settings.BOOTSTRAP_ADMIN_EMAIL or "").strip().lower()
    if not email:
        logger.warning("BOOTSTRAP_ADMIN_EMAIL is not set; skipping admin bootstrap.")
        return None

    existing = firestore_users.get_document_by_field("email", email)
    if existing:
        return existing

    user_id = generate_id()
    admin = {
        "full_name": settings.BOOTSTRAP_ADMIN_NAME,
        "email": email,
        "role": UserRole.ADMIN.value,
        "is_active": True,
        "created_at": datetime.utcnow().isoformat(),
    }
    firestore_users.add_document(str(user_id), admin)
    if settings.BOOTSTRAP_ADMIN_PASSWORD:
        identity.set_password(user_id, settings.BOOTSTRAP_ADMIN_PASSWORD, source="bootstrap", revoke=False)

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
