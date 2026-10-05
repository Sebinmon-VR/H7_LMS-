"""
Wipes the LMS database and re-creates a single administrator.

DESTRUCTIVE. This deletes every document in every LMS collection, including every stored
password, then recreates the bootstrap admin so you can sign in and build real data from an
empty system.

Usage:
    # See exactly what would be deleted, change nothing
    python -m scripts.reset_data --dry-run

    # Wipe everything and recreate the admin
    python -m scripts.reset_data --confirm

Passwords always go with their accounts: user ids are reused after a wipe, and a password
left behind would sign somebody into a stranger's new account.

`--confirm` is mandatory: without it the script refuses to delete anything. There is no
undo.
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402
from app.core.firebase import (  # noqa: E402
    document_cache,
    firestore_attendance,
    firestore_classes,
    firestore_grades,
    firestore_materials,
    firestore_meetings,
    firestore_student_enrollments,
    firestore_subjects,
    firestore_teacher_mappings,
    firestore_topics,
    firestore_user_credentials,
    firestore_users,
    database_backend,
    database_ready,
)
from app.core.concurrency import run_parallel  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

COLLECTIONS = [
    ("attendance_records", firestore_attendance),
    ("exam_grades", firestore_grades),
    ("topics_covered", firestore_topics),
    ("live_meetings", firestore_meetings),
    ("study_materials", firestore_materials),
    ("student_enrollments", firestore_student_enrollments),
    ("teacher_subject_class_mappings", firestore_teacher_mappings),
    ("class_rooms", firestore_classes),
    ("subjects", firestore_subjects),
    ("users", firestore_users),
    ("user_credentials", firestore_user_credentials),
]


def survey() -> dict[str, list]:
    """Reads every collection concurrently and returns its documents."""
    results = run_parallel({name: service.list_all for name, service in COLLECTIONS})
    return {name: (results.get(name) or []) for name, _ in COLLECTIONS}


def wipe_collections(contents: dict[str, list]) -> int:
    deleted = 0
    for name, service in COLLECTIONS:
        docs = contents.get(name, [])
        if not docs:
            continue
        run_parallel({
            str(d["id"]): (lambda s=service, i=d["id"]: s.delete_document(str(i)))
            for d in docs
        })
        deleted += len(docs)
        print(f"  deleted {len(docs):>4} from {name}")
    return deleted


def main() -> int:
    parser = argparse.ArgumentParser(description="Wipe LMS data and recreate the bootstrap admin.")
    parser.add_argument("--confirm", action="store_true",
                        help="Required. Without it nothing is deleted.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be deleted and exit.")
    args = parser.parse_args()

    if not database_ready():
        print("ERROR: the database is unavailable. Check the DB_* settings.")
        return 1

    print(f"\nDatabase: {database_backend()}")
    print("Surveying collections...\n")
    contents = survey()

    total = sum(len(v) for v in contents.values())
    print(f"{'COLLECTION':<36}{'DOCUMENTS':>10}")
    print("-" * 46)
    for name, _ in COLLECTIONS:
        print(f"{name:<36}{len(contents[name]):>10}")
    print("-" * 46)
    print(f"{'TOTAL':<36}{total:>10}")

    if args.dry_run:
        print("\nDry run - nothing was deleted.")
        print("Re-run with --confirm to apply.\n")
        return 0

    if not args.confirm:
        print("\nRefusing to delete without --confirm.")
        print("Review the counts above, then re-run with --confirm.\n")
        return 1

    print(f"\nDeleting {total} document(s)...")
    deleted = wipe_collections(contents)

    document_cache.clear()

    print("\nRecreating the bootstrap administrator...")
    from app.db.init_db import ensure_bootstrap_admin

    admin = ensure_bootstrap_admin()
    if admin:
        print(f"  {settings.BOOTSTRAP_ADMIN_EMAIL}  (role ADMIN, id {admin.get('id')})")
    else:
        print("  WARNING: the admin was not created. Run scripts/provision_auth_user.py.")

    print(f"\nDone. Removed {deleted} document(s).")
    print(f"Sign in as {settings.BOOTSTRAP_ADMIN_EMAIL} and create real data from there.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
