"""
Provisions a single Firebase Authentication account and links it to an LMS profile.

Use this to create a working sign-in for an admin (or any user) without running the full
migration, and to reset a password on an account that already exists.

It is idempotent: run it repeatedly and it will create the Firebase account if missing,
update the password if present, link `firebase_uid` onto the Firestore profile, and refresh
the role custom claims.

Usage:
    # Provision the seeded admin so you can sign in
    python -m scripts.provision_auth_user --email admin@lms.com --password admin123

    # Provision a user who has no LMS profile yet
    python -m scripts.provision_auth_user --email head@yourschool.com --password "S3cret!" \
        --role ADMIN --full-name "Head Teacher"

    # Reset a password
    python -m scripts.provision_auth_user --email teacher.math@lms.com --password newpass123

    # Link an account that already signed in with Google (no password involved)
    python -m scripts.provision_auth_user --email head@yourschool.com --role ADMIN

Omitting --password leaves the credential untouched and only links the profile and claims,
which is what a Google-sign-in account needs.

Note: signing in with a password also requires the Email/Password provider to be enabled in
Firebase Console -> Authentication -> Sign-in method. Creating the account here works either
way, but sign-in will fail with OPERATION_NOT_ALLOWED until that provider is on.
"""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.enums import UserRole  # noqa: E402
from app.core.firebase import firestore_users  # noqa: E402
from app.core.firebase_auth import _auth_module, set_role_claims  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")


def provision(email: str, password: str | None, role: str | None, full_name: str | None) -> int:
    auth = _auth_module()
    if auth is None:
        print("ERROR: Firebase Admin SDK unavailable. Check FIREBASE_CREDENTIALS_PATH.")
        return 1

    if password is not None and len(password) < 6:
        print("ERROR: Firebase requires a password of at least 6 characters.")
        return 1

    # --- Firestore profile ------------------------------------------------
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
        print(f"  Firestore profile CREATED  id={user_id} role={profile['role']}")
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
            print(f"  Firestore profile UPDATED  {updates}")
        else:
            print(f"  Firestore profile FOUND    id={profile['id']} role={profile.get('role')}")

    # --- Firebase Auth account -------------------------------------------
    try:
        record = auth.get_user_by_email(email)
        updates = {"display_name": profile.get("full_name"), "disabled": False}
        if password is not None:
            updates["password"] = password
        auth.update_user(record.uid, **updates)

        providers = [p.provider_id for p in record.provider_data]
        note = "password reset" if password is not None else f"linked, providers={providers}"
        print(f"  Firebase account UPDATED   uid={record.uid} ({note})")
        uid = record.uid
    except auth.UserNotFoundError:
        if password is None:
            print(f"ERROR: No Firebase account exists for '{email}'.")
            print("       Pass --password to create one, or have the user sign in with Google first.")
            return 1
        record = auth.create_user(
            email=email,
            password=password,
            display_name=profile.get("full_name"),
            email_verified=True,
        )
        uid = record.uid
        print(f"  Firebase account CREATED   uid={uid}")
    except Exception as exc:
        print(f"ERROR: could not provision the Firebase account: {exc}")
        return 1

    # --- Link and claims --------------------------------------------------
    if profile.get("firebase_uid") != uid:
        firestore_users.add_document(str(profile["id"]), {"firebase_uid": uid})
        print(f"  Linked firebase_uid        {uid} -> profile {profile['id']}")

    if set_role_claims(uid, profile["role"], int(profile["id"])):
        print(f"  Custom claims SET          role={profile['role']} lms_user_id={profile['id']}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create or update a Firebase Auth account and link it to an LMS profile."
    )
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", help="At least 6 characters. Omit to link a Google "
                                           "sign-in account without touching its credential.")
    parser.add_argument("--role", choices=["ADMIN", "TEACHER", "STUDENT"],
                        help="Required only when no LMS profile exists yet.")
    parser.add_argument("--full-name", dest="full_name")
    args = parser.parse_args()

    print(f"\nProvisioning {args.email}")
    print("-" * 60)
    code = provision(args.email, args.password, args.role, args.full_name)
    print("-" * 60)

    if code == 0:
        if args.password:
            print("Done. Sign in with this email and password.\n")
            print("If sign-in reports the credentials are not recognised, confirm that")
            print("Email/Password is enabled under Firebase Console -> Authentication")
            print("-> Sign-in method.\n")
        else:
            print("Done. This account can now sign in with Google and reach the LMS.\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
