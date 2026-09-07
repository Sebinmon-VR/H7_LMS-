"""
Reconciles the teaching staff roster against Firestore.

The roster below is the school's own record of who teaches what: full legal name, the
personal email each teacher actually signs in with, and the subject/grade combinations they
hold. This script makes the database match it.

It is idempotent and additive. Running it repeatedly performs the same set of changes and
then reports "no change". Teachers absent from ROSTER are never touched, because the roster
is a partial statement about the staff it names, not a complete statement about the school.

Three things happen, in order:

  1. Subjects named in the roster that do not exist yet are created.
  2. Named teachers get their `full_name` and `email` corrected, on both the Firestore
     profile and the linked Firebase Auth account, so the two never drift apart.
  3. Subject/class mappings are brought in line: missing ones are added, and ones the
     roster contradicts are removed.

Passwords are a separate step, because issuing one invalidates whatever the teacher is
using today. Pass --issue-passwords to mint a fresh password per rostered teacher and print
the list. Nothing is emailed: delivery is the admin's call, through the
`POST /admin/users/{id}/generate-credentials` endpoint or by hand.

Usage:
    # See what would change, touching nothing
    python -m scripts.sync_teacher_roster --dry-run

    # Apply the roster
    python -m scripts.sync_teacher_roster

    # Apply it and issue fresh passwords
    python -m scripts.sync_teacher_roster --issue-passwords
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402
from app.core.credentials import generate_password  # noqa: E402
from app.core.enums import TEACHING_ROLE_VALUES  # noqa: E402
from app.core.firebase import (  # noqa: E402
    firestore_classes,
    firestore_subjects,
    firestore_teacher_mappings,
    firestore_users,
    generate_id,
)
from app.core.firebase_auth import (  # noqa: E402
    create_auth_user,
    revoke_tokens,
    set_auth_password,
    set_role_claims,
    update_auth_user,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

# Class codes as they exist in the database, in teaching order. KG1 is stored as "KG-1" and
# KG2 as "KG2"; the inconsistency is already in the data and renaming it would break the
# class teacher mapping and the student enrolments, so the roster spells both out rather
# than deriving them.
KG1, KG2 = "KG-1", "KG2"
KG = [KG1, KG2]


def grades(*specs: str) -> list[str]:
    """Expands "1-3" / "4,5,6,7" style grade specs into class codes."""
    out: list[str] = []
    for spec in specs:
        for part in spec.split(","):
            part = part.strip()
            if "-" in part:
                lo, hi = (int(x) for x in part.split("-"))
                out += [f"G{n}" for n in range(lo, hi + 1)]
            else:
                out.append(f"G{int(part)}")
    return out


KG_TO_7 = KG + grades("1-7")
KG2_TO_7 = [KG2] + grades("1-7")

# Subjects the roster needs that the database may not have yet, keyed by the code used in
# ROSTER below. Codes already present in Firestore are matched, not duplicated.
REQUIRED_SUBJECTS = {
    "RHYM": ("RHYMES & STORIES", "Rhymes and stories (KG)"),
}

# The roster. `subjects` maps a subject code to the class codes that teacher takes it in.
#
# Only the subject codes listed for a teacher are reconciled. A mapping in some *other*
# subject is left alone, so an extra assignment made through the admin UI survives a run.
ROSTER = [
    {
        "match": "Rimsha",
        "full_name": "RIMSHA NAHAS",
        "email": "rimshanahas@gmail.com",
        "subjects": {"MAT": KG, "GK": KG, "RHYM": KG},
    },
    {
        "match": "Sheeja",
        "full_name": "SHEEJA SAID MOHAMMED",
        "email": "sheejasaidmohammed@gmail.com",
        # "Kg1(Eng) & Kg2": English in both KG years, Rhymes & Stories in KG2 only.
        "subjects": {"ENG": KG, "RHYM": [KG2]},
    },
    {
        "match": "Neethu",
        "full_name": "NEETHU K",
        "email": "neethu2812@gmail.com",
        "subjects": {"ENG": grades("1-3"), "GK": grades("1-7")},
    },
    {
        "match": "Riswana",
        "full_name": "RISWANA RAHMAN",
        "email": "riswanasibin@gmail.com",
        "subjects": {"SC": grades("1-3"), "IT": grades("1-3")},
    },
    {
        "match": "Ansilath",
        "full_name": "ANSILATH M",
        "email": "grade3.onlineteacher@gmail.com",
        "subjects": {"MAT": grades("1-7")},
    },
    {
        "match": "Najnin",
        "full_name": "NAJNIN SHIYAS",
        "email": "najninnizar@gmail.com",
        "subjects": {"SST": grades("1-7")},
    },
    {
        "match": "Ruksana",
        "full_name": "RUKSANA MUHAMMED SHAJI",
        "email": "rukzanamuhammedshaji@gmail.com",
        "subjects": {"ENG": grades("4-7"), "SC": grades("4-7")},
    },
    {
        "match": "Deepthi",
        "full_name": "DEEPTHI SREE",
        "email": "deepthisreevinay1992@gmail.com",
        "subjects": {"IT": grades("4-7")},
    },
    {
        "match": "Nissin",
        "full_name": "NISSIN MARY BINU",
        "email": "ammunissin@gmail.com",
        # "VE / Art and Craft" is two subjects in this database, not one.
        "subjects": {"ART-CRAFT": KG_TO_7, "VE-MS": KG_TO_7},
    },
    {
        "match": "Sumayya",
        "full_name": "SUMAYYA",
        "email": "nazlinaiza@gmail.com",
        "subjects": {"ARBC": KG2_TO_7},
    },
    {
        "match": "Fathima",
        "full_name": "FATHIMA KASSIM",
        "email": "fathimakassim2023@gmail.com",
        "subjects": {"ABCS": grades("1-4")},
    },
    {
        "match": "Praveena",
        "full_name": "PRAVEENA KP",
        "email": "kppraveena3390@gmail.com",
        "subjects": {"MSC": KG_TO_7},
    },
    {
        "match": "Lubna",
        "full_name": "LUBNA",
        "email": "lubnasabeelp@gmail.com",
        # "KG & 2" reads as the two KG years: Malayalam in G1-G7 is another teacher's.
        "subjects": {"MAL": KG},
    },
]


class Reconciler:
    def __init__(self, dry_run: bool) -> None:
        self.dry_run = dry_run
        self.changes: list[str] = []
        self.users = firestore_users.list_all()
        self.subjects = {s["code"]: s for s in firestore_subjects.list_all()}
        self.classes = {c["code"]: c for c in firestore_classes.list_all()}
        self.class_code_by_id = {int(c["id"]): c["code"] for c in self.classes.values()}
        self.mappings = firestore_teacher_mappings.list_all()

    def log(self, message: str) -> None:
        self.changes.append(message)
        print(("  WOULD " if self.dry_run else "  ") + message)

    # -- step 1: subjects ---------------------------------------------------------------
    def ensure_subjects(self) -> None:
        print("\nSubjects")
        for code, (name, description) in REQUIRED_SUBJECTS.items():
            if code in self.subjects:
                continue
            subject_id = generate_id()
            record = {"name": name, "code": code, "description": description}
            if not self.dry_run:
                firestore_subjects.add_document(str(subject_id), record)
            self.subjects[code] = {**record, "id": subject_id}
            self.log(f"create subject {code} ({name})")

    # -- step 2: identities -------------------------------------------------------------
    def resolve_teacher(self, entry: dict) -> dict | None:
        """
        Finds the existing profile for a roster entry.

        Matched on the target email first so a re-run is stable once the email has been
        corrected, then on the short first name the accounts were originally created with.
        """
        email = entry["email"].lower()
        for user in self.users:
            if (user.get("email") or "").lower() == email:
                return user

        needle = entry["match"].lower()
        candidates = [
            u for u in self.users
            if u.get("role") in TEACHING_ROLE_VALUES
            and (u.get("full_name") or "").strip().lower() == needle
        ]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            print(f"  !! '{entry['match']}' matches {len(candidates)} profiles; skipping")
        return None

    def sync_identity(self, user: dict, entry: dict) -> None:
        updates = {}
        if (user.get("full_name") or "") != entry["full_name"]:
            updates["full_name"] = entry["full_name"]

        if (user.get("email") or "").lower() != entry["email"].lower():
            clash = firestore_users.get_document_by_field("email", entry["email"].lower())
            if clash and str(clash["id"]) != str(user["id"]):
                print(f"  !! {entry['email']} already belongs to user {clash['id']}; skipping email")
            else:
                updates["email"] = entry["email"].lower()

        if not updates:
            return

        was = f"{user.get('full_name')} <{user.get('email')}>"
        now = (f"{updates.get('full_name', user.get('full_name'))} "
               f"<{updates.get('email', user.get('email'))}>")
        if not self.dry_run:
            firestore_users.add_document(str(user["id"]), updates)
            if user.get("firebase_uid"):
                # Firestore and Firebase Auth hold the email separately. Letting them
                # diverge leaves an account the admin list shows one address for and that
                # signs in with another.
                update_auth_user(
                    user["firebase_uid"],
                    email=updates.get("email"),
                    display_name=updates.get("full_name"),
                )
        user.update(updates)
        self.log(f"update {was} -> {now}")

    # -- step 3: mappings ---------------------------------------------------------------
    def sync_mappings(self, user: dict, entry: dict) -> None:
        teacher_id = int(user["id"])

        for code, class_codes in entry["subjects"].items():
            subject = self.subjects.get(code)
            if not subject:
                print(f"  !! subject '{code}' does not exist; skipping")
                continue
            subject_id = int(subject["id"])

            wanted = set()
            for class_code in class_codes:
                class_room = self.classes.get(class_code)
                if not class_room:
                    print(f"  !! class '{class_code}' does not exist; skipping")
                    continue
                wanted.add(int(class_room["id"]))

            current = {
                int(m["class_id"]): m for m in self.mappings
                if int(m.get("teacher_id", 0)) == teacher_id
                and int(m.get("subject_id", 0)) == subject_id
            }

            for class_id in sorted(wanted - current.keys()):
                mapping_id = generate_id()
                record = {
                    "teacher_id": teacher_id,
                    "subject_id": subject_id,
                    "class_id": class_id,
                }
                if not self.dry_run:
                    firestore_teacher_mappings.add_document(str(mapping_id), record)
                self.mappings.append({**record, "id": mapping_id})
                self.log(f"map {entry['full_name']}: {code} -> "
                         f"{self.class_code_by_id.get(class_id, class_id)}")

            # Only the roster's own subjects are pruned, so the one thing removed here is a
            # class the roster does not list for a subject the roster does list.
            for class_id in sorted(current.keys() - wanted):
                mapping = current[class_id]
                if not self.dry_run:
                    firestore_teacher_mappings.delete_document(str(mapping["id"]))
                self.mappings = [m for m in self.mappings if m["id"] != mapping["id"]]
                self.log(f"unmap {entry['full_name']}: {code} -> "
                         f"{self.class_code_by_id.get(class_id, class_id)} (not in roster)")

    def run(self) -> list[dict]:
        self.ensure_subjects()
        resolved = []

        print("\nIdentities")
        for entry in ROSTER:
            user = self.resolve_teacher(entry)
            if not user:
                print(f"  !! no profile found for {entry['full_name']} ({entry['match']})")
                continue
            self.sync_identity(user, entry)
            resolved.append((user, entry))

        print("\nMappings")
        for user, entry in resolved:
            self.sync_mappings(user, entry)

        return [user for user, _ in resolved]


def issue_passwords(users: list[dict], dry_run: bool) -> list[tuple[dict, str]]:
    """
    Mints a fresh password per teacher and applies it to Firebase Auth.

    The password exists only in this function's return value and the printed table; it is
    never written to Firestore and cannot be read back afterwards.
    """
    issued = []
    for user in sorted(users, key=lambda u: u.get("full_name") or ""):
        password = generate_password(settings.GENERATED_PASSWORD_LENGTH)
        if dry_run:
            issued.append((user, password))
            continue

        uid = user.get("firebase_uid")
        if uid:
            applied = set_auth_password(uid, password)
        else:
            uid = create_auth_user(user["email"], password, user.get("full_name"))
            applied = uid is not None
            if applied:
                firestore_users.add_document(str(user["id"]), {"firebase_uid": uid})

        if not applied:
            print(f"  !! could not set a password for {user.get('email')}")
            continue

        set_role_claims(uid, user.get("role"), int(user["id"]))
        # The rostered email change means the old address may still hold a live session.
        revoke_tokens(uid)
        issued.append((user, password))
    return issued


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report the changes without writing anything")
    parser.add_argument("--issue-passwords", action="store_true",
                        help="mint and print a fresh password for each rostered teacher")
    args = parser.parse_args()

    if args.dry_run:
        print("DRY RUN - nothing will be written.")

    reconciler = Reconciler(args.dry_run)
    users = reconciler.run()

    print(f"\n{len(reconciler.changes)} change(s); "
          f"{len(users)}/{len(ROSTER)} roster entries resolved.")

    if args.issue_passwords:
        print("\nCredentials")
        issued = issue_passwords(users, args.dry_run)
        width = max((len(u.get("full_name") or "") for u, _ in issued), default=4)
        for user, password in issued:
            print(f"  {(user.get('full_name') or ''):<{width}}  "
                  f"{user.get('email'):<34}  {password}")
        print("\nPasswords are shown once and are not recoverable. Nothing was emailed.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
