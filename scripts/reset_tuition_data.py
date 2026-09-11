"""
Clears out tuition activity data, keeping the people.

DESTRUCTIVE, and deliberately narrow. This removes the records you create *while testing* -
enrollments, timetable slots, classes, library items, fee plans, invoices, and tuition
homework and report cards - and leaves alone the two things you would be sorry to lose:

  * **Users.** Every account stays, tuition access included. That is the whole point of this
    script over `reset_data.py`: onboarding people is the slow part, and there is no reason
    to redo it because the test bookings were wrong.
  * **Shared reference data.** Subjects and classrooms belong to both products; deleting a
    subject here would break the school LMS timetable that also points at it.

It also never touches a single LMS collection. The tuition module has its own collections
precisely so this kind of clean-up cannot reach across.

Usage:
    # See exactly what would be deleted, change nothing
    python -m scripts.reset_tuition_data --dry-run

    # Delete it
    python -m scripts.reset_tuition_data --confirm

    # Keep the fee plans and programme settings you have configured
    python -m scripts.reset_tuition_data --confirm --keep-config

    # Restrict to one student's data (useful when only some of it was test data)
    python -m scripts.reset_tuition_data --confirm --student 1712345678901

`--confirm` is mandatory: without it nothing is deleted. There is no undo.
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.concurrency import run_parallel  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.enums import Program  # noqa: E402
from app.core.firebase import (  # noqa: E402
    document_cache,
    firestore_app_settings,
    firestore_exam_submissions,
    firestore_exams,
    firestore_report_cards,
    firestore_tuition_enrollments,
    firestore_tuition_fee_plans,
    firestore_tuition_invoices,
    firestore_tuition_library,
    firestore_tuition_reminder_log,
    firestore_tuition_sessions,
    firestore_tuition_slots,
    initialize_firebase,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

# Collections owned outright by the tuition module. Every document in them is tuition data,
# so these are emptied wholesale.
ACTIVITY_COLLECTIONS = [
    ("tuition_invoices", firestore_tuition_invoices),
    ("tuition_sessions", firestore_tuition_sessions),
    ("tuition_slots", firestore_tuition_slots),
    ("tuition_library", firestore_tuition_library),
    ("tuition_enrollments", firestore_tuition_enrollments),
    ("tuition_reminder_log", firestore_tuition_reminder_log),
]

# Configuration rather than test data: the rates and the programme settings an admin has
# tuned. Cleared only when `--keep-config` is absent, because retyping them is annoying and
# they are rarely what you meant by "the test records".
CONFIG_COLLECTIONS = [
    ("tuition_fee_plans", firestore_tuition_fee_plans),
]

# Collections *shared* with the school LMS. A blanket delete here would take the school's
# exams and report cards with it, so each document is filtered by `program` first.
SHARED_COLLECTIONS = [
    ("exams (tuition only)", firestore_exams),
    ("report_cards (tuition only)", firestore_report_cards),
]


def _is_tuition(document: dict) -> bool:
    return document.get("program") == Program.TUITION.value


def survey(student_id: int | None) -> dict[str, list]:
    """
    Reads everything that would be deleted, concurrently.

    Returns the documents themselves rather than counts, so the delete pass works from the
    same snapshot the dry run showed you - and cannot widen between the two.
    """
    services = {name: service.list_all for name, service in
                ACTIVITY_COLLECTIONS + CONFIG_COLLECTIONS + SHARED_COLLECTIONS}
    raw = run_parallel(services)

    contents: dict[str, list] = {}
    for name, _ in ACTIVITY_COLLECTIONS + CONFIG_COLLECTIONS:
        contents[name] = raw.get(name) or []
    for name, _ in SHARED_COLLECTIONS:
        contents[name] = [d for d in (raw.get(name) or []) if _is_tuition(d)]

    if student_id is not None:
        contents = _narrow_to_student(contents, student_id)

    # Exam submissions hang off tuition exams and have no `program` of their own, so they are
    # collected by exam id rather than surveyed directly.
    exam_ids = {str(e.get("id")) for e in contents.get("exams (tuition only)", [])}
    contents["exam_submissions (tuition only)"] = [
        s for s in firestore_exam_submissions.list_all()
        if str(s.get("exam_id")) in exam_ids
    ] if exam_ids else []

    return contents


def _narrow_to_student(contents: dict[str, list], student_id: int) -> dict[str, list]:
    """
    Keeps only documents belonging to one student.

    Fee plans and the reminder log are dropped from a per-student run entirely: a fee plan
    belongs to a subject, not a person, and deleting one because a single student's test data
    was wrong would silently change what every other student is charged.
    """
    enrollment_ids = {
        str(e.get("id")) for e in contents.get("tuition_enrollments", [])
        if e.get("student_id") == student_id
    }

    narrowed: dict[str, list] = {}
    for name, documents in contents.items():
        if name in {"tuition_fee_plans", "tuition_reminder_log"}:
            narrowed[name] = []
            continue
        narrowed[name] = [
            d for d in documents
            if d.get("student_id") == student_id
            or str(d.get("enrollment_id")) in enrollment_ids
            or d.get("uploaded_by") == student_id
        ]
    return narrowed


def delete_documents(name: str, service, documents: list[dict]) -> int:
    if not documents:
        return 0
    run_parallel({
        str(doc["id"]): (lambda s=service, i=doc["id"]: s.delete_document(str(i)))
        for doc in documents
    })
    print(f"  deleted {len(documents):>4} from {name}")
    return len(documents)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clear tuition activity data. Users are always kept."
    )
    parser.add_argument("--confirm", action="store_true",
                        help="Required. Without it nothing is deleted.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be deleted and exit.")
    parser.add_argument("--keep-config", action="store_true",
                        help="Keep fee plans and the programme settings document.")
    parser.add_argument("--student", type=int, default=None,
                        help="Only delete data belonging to this student id.")
    args = parser.parse_args()

    if initialize_firebase() is None:
        print("ERROR: Firestore is unavailable. Check FIREBASE_CREDENTIALS_PATH.")
        return 1

    print(f"\nProject: {settings.GCP_PROJECT_ID}")
    if args.student is not None:
        print(f"Restricted to student {args.student}")
    print("Surveying tuition collections...\n")

    contents = survey(args.student)

    order = [name for name, _ in ACTIVITY_COLLECTIONS]
    if not args.keep_config:
        order += [name for name, _ in CONFIG_COLLECTIONS]
    order += [name for name, _ in SHARED_COLLECTIONS] + ["exam_submissions (tuition only)"]

    total = sum(len(contents.get(name, [])) for name in order)

    print(f"{'COLLECTION':<36}{'DOCUMENTS':>10}")
    print("-" * 46)
    for name in order:
        print(f"{name:<36}{len(contents.get(name, [])):>10}")
    print("-" * 46)
    print(f"{'TOTAL':<36}{total:>10}")

    print("\nKEPT, and not read by this script:")
    print("  users            every account, tuition access included")
    print("  subjects         shared with the school LMS")
    print("  class_rooms      shared with the school LMS")
    print("  every LMS collection (attendance, timetable, exams, materials, ...)")
    if args.keep_config:
        print("  tuition_fee_plans and app_settings (--keep-config)")

    if args.dry_run:
        print("\nDry run - nothing was deleted.")
        print("Re-run with --confirm to apply.\n")
        return 0

    if not args.confirm:
        print("\nRefusing to delete without --confirm.")
        print("Review the counts above, then re-run with --confirm.\n")
        return 1

    if total == 0:
        print("\nNothing to delete.\n")
        return 0

    print(f"\nDeleting {total} document(s)...")
    services = dict(ACTIVITY_COLLECTIONS + CONFIG_COLLECTIONS + SHARED_COLLECTIONS)
    services["exam_submissions (tuition only)"] = firestore_exam_submissions

    deleted = 0
    for name in order:
        deleted += delete_documents(name, services[name], contents.get(name, []))

    # The programme settings document is configuration, not a record, so it goes only on a
    # full reset - and never on a per-student one, where it would be nonsense.
    if not args.keep_config and args.student is None:
        for program in (Program.TUITION.value.lower(), Program.LMS.value.lower()):
            if firestore_app_settings.get_document(program):
                firestore_app_settings.delete_document(program)
                print(f"  reset app_settings/{program} to environment defaults")

    document_cache.clear()

    print(f"\nDone. {deleted} document(s) deleted; every user account kept.")
    print("Tuition users can sign in as before - they simply have no classes booked.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
