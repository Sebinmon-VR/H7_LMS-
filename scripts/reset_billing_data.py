"""
Clears every billing record, in both products, keeping the people and the classes.

DESTRUCTIVE, and deliberately narrow. This removes what the fee modules have *produced* -
invoices, payments recorded on them, gateway intents, and the tuition packages and package
assignments those invoices were priced from - and leaves alone everything they were priced
*about*: users, enrollments, sessions and attendance are untouched, so the same classes can
be re-billed under the new package rules the moment a package is assigned.

Two tiers, because "the billing data" means two different things to two different people:

  * The default clears the **records**: `tuition_invoices`, `tuition_package_assignments`,
    `tuition_packages`, the legacy `tuition_fee_plans`, `fee_invoices` and `payment_intents`.
  * `--include-structure` also clears the school's **price list**: `fee_structures`,
    `instalment_plans`, `discount_rules` and `fee_heads`. Off by default because a price
    list is configuration somebody typed in, not test output.

Usage:
    python -m scripts.reset_billing_data --dry-run                       # report only
    python -m scripts.reset_billing_data --confirm                       # clear the records
    python -m scripts.reset_billing_data --confirm --include-structure   # and the price list

`--confirm` is mandatory: without it nothing is deleted. There is no undo.
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.concurrency import run_parallel  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.firebase import (  # noqa: E402
    document_cache,
    firestore_discount_rules,
    firestore_fee_heads,
    firestore_fee_invoices,
    firestore_fee_structures,
    firestore_instalment_plans,
    firestore_payment_intents,
    firestore_tuition_fee_plans,
    firestore_tuition_invoices,
    firestore_tuition_package_assignments,
    firestore_tuition_packages,
    initialize_firebase,
)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

# What the fee modules produced. Deleted in this order so nothing points at a record that
# has already gone: intents before the invoices they reference, assignments before the
# packages they name.
RECORD_COLLECTIONS = [
    ("payment_intents", firestore_payment_intents),
    ("fee_invoices", firestore_fee_invoices),
    ("tuition_invoices", firestore_tuition_invoices),
    ("tuition_package_assignments", firestore_tuition_package_assignments),
    ("tuition_packages", firestore_tuition_packages),
    ("tuition_fee_plans (legacy)", firestore_tuition_fee_plans),
]

# The school's price list. Configuration rather than output; cleared only on request.
STRUCTURE_COLLECTIONS = [
    ("instalment_plans", firestore_instalment_plans),
    ("discount_rules", firestore_discount_rules),
    ("fee_structures", firestore_fee_structures),
    ("fee_heads", firestore_fee_heads),
]


def survey(collections: list[tuple]) -> dict[str, list]:
    """
    Reads everything that would be deleted, concurrently.

    Returns the documents themselves rather than counts, so the delete pass works from the
    same snapshot the dry run showed you - and cannot widen between the two.
    """
    raw = run_parallel({name: service.list_all for name, service in collections})
    return {name: raw.get(name) or [] for name, _ in collections}


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
        description="Clear billing records in both products. Users and classes are always kept."
    )
    parser.add_argument("--confirm", action="store_true",
                        help="Required. Without it nothing is deleted.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be deleted and exit.")
    parser.add_argument("--include-structure", action="store_true",
                        help="Also clear the school's fee heads, structures, instalment "
                             "plans and discount rules.")
    args = parser.parse_args()

    if initialize_firebase() is None:
        print("ERROR: Firestore is unavailable. Check FIREBASE_CREDENTIALS_PATH.")
        return 1

    collections = list(RECORD_COLLECTIONS)
    if args.include_structure:
        collections += STRUCTURE_COLLECTIONS

    print(f"\nProject: {settings.GCP_PROJECT_ID}")
    print("Surveying billing collections...\n")
    contents = survey(collections)
    total = sum(len(rows) for rows in contents.values())

    print(f"{'COLLECTION':<36}{'DOCUMENTS':>10}")
    print("-" * 46)
    for name, _ in collections:
        print(f"{name:<36}{len(contents[name]):>10}")
    print("-" * 46)
    print(f"{'TOTAL':<36}{total:>10}")

    print("\nKEPT, and not read by this script:")
    print("  users, tuition_enrollments, tuition_sessions, tuition_slots - the people and")
    print("  the classes; re-billable under the new packages once one is assigned")
    if not args.include_structure:
        print("  fee_heads, fee_structures, instalment_plans, discount_rules - the school's")
        print("  price list (pass --include-structure to clear these too)")
    print("  app_settings - currencies, tax and gateway settings")

    if args.dry_run or not args.confirm:
        if not args.dry_run:
            print("\nNothing deleted. Re-run with --confirm to delete the documents above.")
        else:
            print("\nDry run: nothing deleted.")
        return 0

    if total == 0:
        print("\nNothing to delete.")
        return 0

    print("\nDeleting...")
    deleted = 0
    for name, service in collections:
        deleted += delete_documents(name, service, contents[name])
    document_cache.clear()
    print(f"\nDone: {deleted} document(s) deleted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
