"""
Copies every collection from Firestore into the Azure SQL document tables.

One-way and additive: nothing in Firestore is changed, and a document already present in
SQL under the same id is overwritten with the Firestore copy rather than skipped, so the
script can be re-run to bring SQL up to date until the switch is thrown. Nothing outside the
application's own schema (`DB_SCHEMA`) is touched.

Reads Firestore directly, not through the switched services in `app.core.firebase` - those
already point at SQL once `DATABASE_BACKEND=azuresql`, and a migration that copied SQL into
SQL would report success while moving nothing.

Usage:
    python -m scripts.migrate_firestore_to_sql --dry-run              # count, write nothing
    python -m scripts.migrate_firestore_to_sql                        # copy everything
    python -m scripts.migrate_firestore_to_sql --collections users,subjects
    python -m scripts.migrate_firestore_to_sql --verify               # compare counts only

Every document read from Firestore counts against its daily quota, so a full copy is one
read per document - run it once, not as a habit.
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core import firebase, sqldb  # noqa: E402
from app.core.config import settings  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")


def collection_names() -> list[str]:
    return [service.collection_name for service in firebase.all_collections()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Copy Firestore collections into Azure SQL.")
    parser.add_argument("--dry-run", action="store_true", help="Count the source and write nothing.")
    parser.add_argument("--verify", action="store_true", help="Compare document counts only.")
    parser.add_argument("--collections", default="", help="Comma-separated subset to copy.")
    args = parser.parse_args()

    if not sqldb.is_configured():
        print("ERROR: Azure SQL is not configured (DB_SERVER, DB_NAME, DB_USER, DB_PASSWORD).")
        return 1
    if firebase.initialize_firebase() is None:
        print("ERROR: Firestore is unavailable. Check FIREBASE_CREDENTIALS_PATH.")
        return 1

    names = collection_names()
    if args.collections:
        wanted = {n.strip() for n in args.collections.split(",") if n.strip()}
        unknown = wanted - set(names)
        if unknown:
            print(f"ERROR: unknown collection(s): {', '.join(sorted(unknown))}")
            return 1
        names = [n for n in names if n in wanted]

    print(f"\nFirestore project {settings.GCP_PROJECT_ID}  ->  "
          f"{settings.DB_SERVER}/{settings.DB_NAME} schema [{settings.DB_SCHEMA}]")
    mode = "VERIFY" if args.verify else "DRY RUN" if args.dry_run else "COPY"
    print(f"Mode: {mode}\n")

    if not args.dry_run:
        sqldb.prepare_schema(names)

    print(f"{'COLLECTION':<34}{'FIRESTORE':>10}{'SQL BEFORE':>12}{'WRITTEN':>9}{'SQL AFTER':>11}")
    print("-" * 76)
    total_source = total_written = 0
    mismatches = []

    for name in names:
        source = firebase.FirestoreService(name)            # uncached, straight from Firestore
        target = sqldb.SqlDocumentService(name, id_factory=firebase.generate_id)

        before = target.count() if not args.dry_run else 0
        if args.verify:
            src_count = len(source.list_all())
            print(f"{name:<34}{src_count:>10}{before:>12}{'-':>9}{before:>11}")
            total_source += src_count
            if src_count != before:
                mismatches.append(name)
            continue

        documents = source.list_all()
        total_source += len(documents)
        written = 0
        if not args.dry_run:
            for document in documents:
                doc_id = str(document.pop("id"))
                target.put_document(doc_id, document)
                written += 1
        after = target.count() if not args.dry_run else 0
        total_written += written
        print(f"{name:<34}{len(documents):>10}{before:>12}{written:>9}{after:>11}")
        if not args.dry_run and after < len(documents):
            mismatches.append(name)

    print("-" * 76)
    print(f"{'TOTAL':<34}{total_source:>10}{'':>12}{total_written:>9}")

    if mismatches:
        print(f"\nCounts differ for: {', '.join(mismatches)}")
        return 2
    if args.dry_run:
        print("\nDry run: nothing written.")
    elif args.verify:
        print("\nEvery collection's SQL count matches Firestore.")
    else:
        print("\nDone. Set DATABASE_BACKEND=azuresql to serve from Azure SQL; the Firestore "
              "code stays in place and DATABASE_BACKEND=firestore switches back.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
