"""
Copies every password out of Firebase Auth into the LMS's own `user_credentials`, so nobody
has to reset their password when sign-in moves off Firebase.

Firebase stores passwords as salted, modified-scrypt hashes and will hand them - and the
project's hash parameters - to its own service account. Nothing is decrypted: the hashes are
copied as they are, the parameters are stored beside them, and the backend checks a sign-in
against them with Firebase's own algorithm (app/core/identity.py). The first time each person
signs in, their hash is replaced by a native one and Firebase is no longer involved at all.

Run it while the Firebase project still answers - it is the one step of the move off Google
that cannot be done afterwards.

Usage:
    python -m scripts.import_firebase_passwords            # report what would be imported
    python -m scripts.import_firebase_passwords --apply    # import
    python -m scripts.import_firebase_passwords --apply --overwrite
        # also replace passwords already stored here (normally kept: somebody who set a new
        # password in the LMS after the move must not have it overwritten by the old one)

Needs the Google libraries, which the Azure deployment no longer installs:
    pip install firebase-admin
and FIREBASE_CREDENTIALS_PATH pointing at the project's service-account key.

Idempotent: a second run imports only what the first did not.
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import identity  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.firebase import (  # noqa: E402
    firestore_user_credentials, firestore_users, initialize_firebase,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")


def _hash_config() -> dict:
    """The project's password-hash parameters, from the Identity Toolkit admin API."""
    import google.auth.transport.requests
    import httpx
    from google.oauth2 import service_account

    path = settings.FIREBASE_CREDENTIALS_PATH
    credentials = service_account.Credentials.from_service_account_file(
        path, scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    credentials.refresh(google.auth.transport.requests.Request())
    with open(path, encoding="utf-8") as handle:
        project_id = json.load(handle)["project_id"]

    response = httpx.get(
        f"https://identitytoolkit.googleapis.com/admin/v2/projects/{project_id}/config",
        headers={"Authorization": f"Bearer {credentials.token}"},
        timeout=30,
    )
    response.raise_for_status()
    config = (response.json().get("signIn") or {}).get("hashConfig") or {}
    if config.get("algorithm") != "SCRYPT" or not config.get("signerKey"):
        raise RuntimeError(f"Unexpected hash configuration: algorithm={config.get('algorithm')!r}")
    return {
        "signer_key": config["signerKey"],
        "salt_separator": config.get("saltSeparator") or "",
        "rounds": int(config.get("rounds") or 8),
        "mem_cost": int(config.get("memoryCost") or 14),
        "project_id": project_id,
        "imported_at": datetime.utcnow().isoformat(),
    }


def _firebase_users() -> list:
    from firebase_admin import auth

    users, page = [], auth.list_users()
    while page:
        users.extend(page.users)
        page = page.get_next_page()
    return users


def main(apply: bool, overwrite: bool) -> int:
    if initialize_firebase() is None:
        print("ERROR: Firebase Admin SDK unavailable. Install firebase-admin and check "
              "FIREBASE_CREDENTIALS_PATH.")
        return 1

    config = _hash_config()
    firebase_users = _firebase_users()
    lms_by_email = {
        str(u.get("email") or "").strip().lower(): u
        for u in firestore_users.list_all() if u.get("email")
    }

    planned, kept, no_profile, no_password = [], [], [], 0
    for record in firebase_users:
        if not record.password_hash or not record.password_salt:
            no_password += 1
            continue
        profile = lms_by_email.get((record.email or "").strip().lower())
        if profile is None:
            no_profile.append(record.email)
            continue
        if identity.has_password(profile["id"]) and not overwrite:
            kept.append(record.email)
            continue
        planned.append((profile, record))

    # A self-check that needs no secret from anybody: the bootstrap admin's password is in
    # configuration, so if that account still uses it the imported hash must verify.
    self_check = "not possible (the bootstrap admin's password was changed, or it has none)"
    admin_email = (settings.BOOTSTRAP_ADMIN_EMAIL or "").strip().lower()
    for record in firebase_users:
        if (record.email or "").lower() == admin_email and record.password_hash and settings.BOOTSTRAP_ADMIN_PASSWORD:
            produced = identity.firebase_scrypt(settings.BOOTSTRAP_ADMIN_PASSWORD, record.password_salt, config)
            if produced == identity._b64decode(record.password_hash):
                self_check = "passed - an imported hash verified with Firebase's algorithm"
            break

    print(f"Firebase accounts:                 {len(firebase_users)}")
    print(f"  with no password (never issued): {no_password}")
    print(f"  with no LMS profile:             {len(no_profile)}")
    print(f"  already have a password here:    {len(kept)} (kept)")
    print(f"  to import:                       {len(planned)}")
    print(f"Hash algorithm:                    SCRYPT, rounds {config['rounds']}, mem cost {config['mem_cost']}")
    print(f"Self-check:                        {self_check}")
    if no_profile:
        print("Firebase accounts with no LMS profile (not imported):")
        for email in sorted(e or "(no email)" for e in no_profile):
            print(f"  - {email}")

    if not apply:
        print("\nDry run - nothing written. Re-run with --apply to import.")
        return 0

    firestore_user_credentials.add_document(identity.FIREBASE_HASH_CONFIG_ID, config)
    for profile, record in planned:
        identity.store_password_hash(
            profile["id"],
            identity.firebase_hash(record.password_hash, record.password_salt),
            source="firebase_import",
        )
        firestore_user_credentials.add_document(str(profile["id"]), {"firebase_uid": record.uid})
    print(f"\nImported {len(planned)} password(s). They keep working exactly as before.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Write the imported hashes.")
    parser.add_argument("--overwrite", action="store_true",
                        help="Also replace passwords already stored in the LMS.")
    args = parser.parse_args()
    sys.exit(main(apply=args.apply, overwrite=args.overwrite))
