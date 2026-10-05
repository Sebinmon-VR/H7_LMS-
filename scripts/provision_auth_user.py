"""
Creates or repairs one sign-in: the LMS profile, and optionally its password.

Use this to get an administrator in without the admin screens - a fresh database, a
forgotten password on the only admin, a locked account.

It is idempotent: run it repeatedly and it creates the profile if missing, reactivates it,
and sets the password when one is given (signing that account out everywhere else).

Usage:
    # Give the seeded admin a password so you can sign in
    python -m scripts.provision_auth_user --email admin@lms.com --password "S3cret-pass"

    # Create an account that has no LMS profile yet
    python -m scripts.provision_auth_user --email head@yourschool.com --password "S3cret-pass" \\
        --role ADMIN --full-name "Head Teacher"

    # Reset a password
    python -m scripts.provision_auth_user --email teacher.math@lms.com --password "newpass123"

Omitting --password leaves the current password untouched and only creates or reactivates
the profile.
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import identity  # noqa: E402
from app.core.enums import UserRole  # noqa: E402
from app.core.firebase import firestore_users  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")


def provision(email: str, password: str | None, role: str | None, full_name: str | None) -> int:
    email = email.strip().lower()
    if password is not None:
        problem = identity.password_problem(password)
        if problem:
            print(f"ERROR: {problem}")
            return 1

    profile = firestore_users.get_document_by_field("email", email)
    if profile is None:
        if not role:
            print(f"ERROR: No LMS profile exists for '{email}'.")
            print("       Pass --role (ADMIN|TEACHER|STUDENT) to create one.")
            return 1

        user_id = firestore_users.get_next_numeric_id()
        profile = {
            "id": user_id,
            "full_name": full_name or email.split("@")[0],
            "email": email,
            "role": UserRole(role.upper()).value,
            "is_active": True,
            "created_at": datetime.utcnow().isoformat(),
        }
        firestore_users.add_document(str(user_id), profile)
        print(f"  Profile CREATED   id={user_id} role={profile['role']}")
    else:
        updates = {}
        if role and profile.get("role") != UserRole(role.upper()).value:
            updates["role"] = UserRole(role.upper()).value
        if full_name and profile.get("full_name") != full_name:
            updates["full_name"] = full_name
        if not profile.get("is_active", False):
            updates["is_active"] = True
        if updates:
            firestore_users.add_document(str(profile["id"]), updates)
            profile.update(updates)
            print(f"  Profile UPDATED   {updates}")
        else:
            print(f"  Profile FOUND     id={profile['id']} role={profile.get('role')}")

    if password is not None:
        identity.set_password(profile["id"], password, source="provisioned", revoke=True)
        print("  Password SET      (other sessions of this account are signed out)")
    elif not identity.has_password(profile["id"]):
        print("  No password yet   - pass --password, or use Generate credentials in the admin screens.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or repair one LMS sign-in.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", help="Omit to leave the current password untouched.")
    parser.add_argument("--role", choices=["ADMIN", "TEACHER", "STUDENT"],
                        help="Required only when no LMS profile exists yet.")
    parser.add_argument("--full-name", dest="full_name")
    args = parser.parse_args()

    print(f"\nProvisioning {args.email}")
    print("-" * 60)
    code = provision(args.email, args.password, args.role, args.full_name)
    print("-" * 60)
    if code == 0 and args.password:
        print("Done. Sign in with this email and password.\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
