"""
Erasing demo accounts once they expire.

A demo account works through its `demo_expires_on` date (app/core/demo.py refuses it from
the next morning). The maintenance sweep then calls `purge_expired`, which removes the
account and every record it left behind - enrollments, attendance, submissions, tuition
classes, game progress - because a trial that ended is not school history worth keeping,
and a half-deleted user leaves rosters pointing at nobody.

Only accounts flagged `is_demo` are ever touched, and an account the office converted to a
real one (flag switched off) is out of reach for good.
"""

import logging
from datetime import date

from app.core import demo, identity
from app.core import firebase as fb
from app.core.storage import forget_profile_photo

logger = logging.getLogger("demo_accounts")


def _references() -> list[tuple[str, object, str]]:
    """Every collection that can hold a row belonging to a demo student, and the field."""
    return [
        ("student enrollments", fb.firestore_student_enrollments, "student_id"),
        ("attendance", fb.firestore_attendance, "student_id"),
        ("class room attendance", fb.firestore_class_room_attendance, "user_id"),
        ("class room events", fb.firestore_class_room_events, "user_id"),
        ("exam grades", fb.firestore_grades, "student_id"),
        ("exam submissions", fb.firestore_exam_submissions, "student_id"),
        ("homework submissions", fb.firestore_homework_submissions, "student_id"),
        ("report cards", fb.firestore_report_cards, "student_id"),
        ("parent links", fb.firestore_parent_links, "student_id"),
        ("tuition enrollments", fb.firestore_tuition_enrollments, "student_id"),
        ("tuition slots", fb.firestore_tuition_slots, "student_id"),
        ("tuition sessions", fb.firestore_tuition_sessions, "student_id"),
        ("tuition package assignments", fb.firestore_tuition_package_assignments, "student_id"),
        ("tuition invoices", fb.firestore_tuition_invoices, "student_id"),
        ("fee invoices", fb.firestore_fee_invoices, "student_id"),
        ("arena points", fb.firestore_arena_points, "student_id"),
        ("lab attempts", fb.firestore_lab_attempts, "student_id"),
        ("presence", fb.firestore_user_presence, "user_id"),
    ]


def erase(user: dict) -> dict:
    """Removes one demo account and what it left behind. Refuses anything not a demo."""
    if not user.get("is_demo"):
        raise ValueError(f"User {user.get('id')} is not a demo account")
    user_id = int(user["id"])
    removed: dict[str, int] = {}
    for label, service, field in _references():
        try:
            rows = service.query_documents(field, "==", user_id)
        except Exception as exc:  # a collection that cannot be read must not stop the rest
            logger.warning("Demo purge could not read %s for user %s: %s", label, user_id, exc)
            continue
        for row in rows:
            try:
                service.delete_document(str(row["id"]))
            except Exception as exc:
                logger.warning("Demo purge could not delete %s %s: %s", label, row.get("id"), exc)
        if rows:
            removed[label] = len(rows)
    # Game progress is keyed by the student's id rather than carrying it as a field.
    try:
        if fb.firestore_arena_profiles.delete_document(str(user_id)):
            removed["arena profile"] = 1
    except Exception as exc:
        logger.warning("Demo purge could not delete the arena profile of %s: %s", user_id, exc)

    identity.revoke_sessions(user_id)
    if identity.has_password(user_id):
        identity.delete_credentials(user_id)
    fb.firestore_users.delete_document(str(user_id))
    forget_profile_photo(user.get("photo_url"))
    logger.info("Erased expired demo account %s (%s): %s", user_id, user.get("email"), removed or "nothing else")
    return {"user_id": user_id, "email": user.get("email"), "removed": removed}


def expired_accounts(today: date | None = None) -> list[dict]:
    return [u for u in fb.firestore_users.list_all() if demo.is_expired(u, today)]


def purge_expired(today: date | None = None) -> list[dict]:
    """Erase every demo account past its expiry. Safe to run at any frequency."""
    erased = []
    for user in expired_accounts(today):
        try:
            erased.append(erase(user))
        except Exception as exc:  # pragma: no cover - one bad account must not stop the sweep
            logger.exception("Could not erase demo account %s: %s", user.get("id"), exc)
    return erased


def summary(today: date | None = None) -> list[dict]:
    """The demo accounts that exist, soonest to expire first - for the admin's list."""
    today = today or demo._today()
    rows = []
    for user in fb.firestore_users.list_all():
        if not user.get("is_demo"):
            continue
        ends = demo.expires_on(user)
        rows.append({
            "id": int(user["id"]),
            "full_name": user.get("full_name"),
            "email": user.get("email"),
            "role": user.get("role"),
            "programs": user.get("programs") or ["LMS"],
            "demo_expires_on": ends.isoformat() if ends else None,
            "days_left": (ends - today).days if ends else None,
            "demo_modules": user.get("demo_modules") or [],
            "expired": demo.is_expired(user, today),
        })
    return sorted(rows, key=lambda r: r["demo_expires_on"] or "9999")
