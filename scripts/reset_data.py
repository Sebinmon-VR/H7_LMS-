"""
Wipes the LMS database and re-creates a single administrator.

DESTRUCTIVE. This deletes every document in every LMS collection, and optionally every
Firebase Authentication account, then recreates the bootstrap admin so you can sign in and
build real data from an empty system.

Usage:
    # See exactly what would be deleted, change nothing
    python -m scripts.reset_data --dry-run

    # Wipe Firestore collections, keep Firebase Auth accounts, recreate the admin
    python -m scripts.reset_data --confirm

    # Also delete every Firebase Auth account (full clean slate)
    python -m scripts.reset_data --confirm --include-auth

`--confirm` is mandatory: without it the script refuses to delete anything. There is no
undo, and Firestore has no built-in restore for this.
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
    firestore_users,
    initialize_firebase,
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


def wipe_auth_accounts() -> int:
    from firebase_admin import auth

    accounts = list(auth.list_users().iterate_all())
    if not accounts:
        return 0

    uids = [u.uid for u in accounts]
    for start in range(0, len(uids), 1000):  # delete_users caps at 1000 per call
        auth.delete_users(uids[start:start + 1000])

    for account in accounts:
        print(f"  deleted auth account {account.email or account.uid}")
    return len(accounts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Wipe LMS data and recreate the bootstrap admin.")
    parser.add_argument("--confirm", action="store_true",
                        help="Required. Without it nothing is deleted.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be deleted and exit.")
    parser.add_argument("--include-auth", action="store_true",
                        help="Also delete every Firebase Authentication account.")
    args = parser.parse_args()

    if initialize_firebase() is None:
        print("ERROR: Firestore is unavailable. Check FIREBASE_CREDENTIALS_PATH.")
        return 1

    print(f"\nProject: {settings.GCP_PROJECT_ID}")
    print("Surveying collections...\n")
    contents = survey()

    total = sum(len(v) for v in contents.values())
    print(f"{'COLLECTION':<36}{'DOCUMENTS':>10}")
    print("-" * 46)
    for name, _ in COLLECTIONS:
        print(f"{name:<36}{len(contents[name]):>10}")
    print("-" * 46)
    print(f"{'TOTAL':<36}{total:>10}")

    auth_count = 0
    if args.include_auth:
        try:
            from firebase_admin import auth
            auth_count = sum(1 for _ in auth.list_users().iterate_all())
            print(f"\nFirebase Auth accounts to delete: {auth_count}")
        except Exception as exc:
            print(f"\nCould not enumerate Firebase Auth accounts: {exc}")

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

    if args.include_auth:
        print(f"\nDeleting {auth_count} Firebase Auth account(s)...")
        try:
            wipe_auth_accounts()
        except Exception as exc:
            print(f"  ERROR deleting auth accounts: {exc}")

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
