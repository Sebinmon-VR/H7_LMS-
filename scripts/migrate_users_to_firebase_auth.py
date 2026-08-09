"""
Migrates existing Firestore user profiles onto Firebase Authentication.

For every user document that has no `firebase_uid`, this finds or creates the matching
Firebase Auth account by email, writes the custom role claims, links the uid back onto the
profile, and removes the now-unused `hashed_password` field.

Usage:
    python -m scripts.migrate_users_to_firebase_auth --dry-run     # report only, no writes
    python -m scripts.migrate_users_to_firebase_auth               # apply
    python -m scripts.migrate_users_to_firebase_auth --keep-hashes # apply, leave hashes in place

Passwords are NOT carried over. Accounts created here have no password until the user either
signs in with Google Workspace or completes a Firebase password reset. To preserve existing
passwords instead, import the bcrypt hashes first with the Firebase CLI:

    firebase auth:import users.json --hash-algo=BCRYPT --project lms-backend-project-955c6

then run this script, which will link the already-imported accounts rather than create new ones.
"""

import argparse
import logging
import sys
from pathlib import Path

# Allow running as a plain script from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.firebase import firestore_users  # noqa: E402
from app.core.firebase_auth import (  # noqa: E402
    create_auth_user, get_auth_user_by_email, is_available, set_role_claims
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("migrate_users")


def migrate(dry_run: bool = False, keep_hashes: bool = False) -> int:
    """Runs the migration. Returns a process exit code."""
    if not is_available():
        logger.error(
            "Firebase Admin SDK is not available. Check FIREBASE_CREDENTIALS_PATH and that "
            "the firebase-admin package is installed."
        )
        return 1

    users = firestore_users.list_all()
    if not users:
        logger.warning("No user documents found in Firestore. Nothing to migrate.")
        return 0

    logger.info("Found %d user document(s).", len(users))
    print()
    print(f"{'ID':<16}{'EMAIL':<36}{'ROLE':<10}ACTION")
    print("-" * 90)

    created = linked = skipped = failed = 0

    for user in sorted(users, key=lambda u: str(u.get("id"))):
        user_id = user.get("id")
        email = user.get("email")
        role = user.get("role")

        if not email:
            print(f"{str(user_id):<16}{'(no email)':<36}{str(role):<10}SKIP - missing email")
            skipped += 1
            continue

        if user.get("firebase_uid"):
            print(f"{str(user_id):<16}{email:<36}{str(role):<10}SKIP - already linked")
            skipped += 1
            continue

        already_in_auth = get_auth_user_by_email(email) is not None
        action = "LINK existing auth account" if already_in_auth else "CREATE auth account"

        if dry_run:
            print(f"{str(user_id):<16}{email:<36}{str(role):<10}would {action}")
            if already_in_auth:
                linked += 1
            else:
                created += 1
            continue

        uid = create_auth_user(email, password=None, display_name=user.get("full_name"))
        if not uid:
            print(f"{str(user_id):<16}{email:<36}{str(role):<10}FAILED")
            failed += 1
            continue

        updates = {"firebase_uid": uid}
        if not keep_hashes and "hashed_password" in user:
            # Firestore deletes a field when it is set to the DELETE_FIELD sentinel.
            try:
                from firebase_admin import firestore as admin_firestore
                updates["hashed_password"] = admin_firestore.DELETE_FIELD
            except Exception:
                updates["hashed_password"] = None

        firestore_users.add_document(str(user_id), updates)
        set_role_claims(uid, role, int(user_id))

        print(f"{str(user_id):<16}{email:<36}{str(role):<10}{action} -> {uid}")
        if already_in_auth:
            linked += 1
        else:
            created += 1

    print("-" * 90)
    verb = "would be" if dry_run else "were"
    print(f"{created} account(s) {verb} created, {linked} linked, {skipped} skipped, {failed} failed.")

    if dry_run:
        print("\nDry run - no changes were written. Re-run without --dry-run to apply.")
    elif created or linked:
        print(
            "\nDone. Users have no password in Firebase yet: have them sign in with Google "
            "Workspace, or send Firebase password-reset emails."
        )

    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Link Firestore users to Firebase Authentication.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing.")
    parser.add_argument("--keep-hashes", action="store_true", help="Leave legacy hashed_password fields in place.")
    args = parser.parse_args()

    return migrate(dry_run=args.dry_run, keep_hashes=args.keep_hashes)


if __name__ == "__main__":
    raise SystemExit(main())
