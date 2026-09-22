"""
Records the admission fee already paid by every student who was on the roll before the
fee module existed, so the collections report can show it.

Those students sit in an admission category that waives the admission charge - that is how
"already paid" is expressed on their bill - but nothing in the system says the money was
received, so the cash book reads zero. This writes one OPENING_BALANCE receipt per such
student for the admission-fee head, and skips anyone who already has one, so it can be run
again safely.

What each receipt says, and where it comes from:

  * amount   - the admission-fee head's default amount, or `--amount`
  * date     - the first of these that exists: a date for the student in `--csv`; the
               `admission_date` on their profile; `--paid-on`; the start of their admission
               year. The note on the receipt says which one was used, because a date that was
               not recorded at the time should not later look as if it had been.
  * year     - the student's admission year (`admission_year_id`, or the year their admission
               date falls in, or the year they are in now). A student assigned to no year is
               skipped and listed, since nothing says which year the fee belonged to.
  * method   - OTHER, reference OPENING, unless the CSV says otherwise

Usage:
    python -m scripts.backfill_admission_receipts                      # dry run: the plan
    python -m scripts.backfill_admission_receipts --apply              # write the receipts
    python -m scripts.backfill_admission_receipts --apply --paid-on 2025-08-01
    python -m scripts.backfill_admission_receipts --csv dates.csv --apply
    python -m scripts.backfill_admission_receipts --undo               # what would be removed
    python -m scripts.backfill_admission_receipts --undo --apply       # remove them

The CSV has a header and any of these columns: student_id or admission_number to pick the
student; paid_on (YYYY-MM-DD); amount; method; reference. Rows for students not on the
plan are ignored.

Nothing here touches invoices, categories or what anyone owes. `--undo` removes only the
OPENING_BALANCE receipts for the admission-fee head that this script (or the equivalent
API call) created.
"""

import argparse
import csv
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.enums import PaymentMethod, Program  # noqa: E402
from app.core.firebase import (  # noqa: E402
    firestore_academic_years, firestore_admission_categories, firestore_fee_heads,
    firestore_users, initialize_firebase,
)
from app.schemas.finance import FeeReceiptCreate  # noqa: E402
from app.services import receipts  # noqa: E402
from app.services.finance import _as_date, admission_year_of, money  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

PROGRAM = Program.LMS.value


def admission_head() -> dict:
    heads = [
        h for h in firestore_fee_heads.list_all()
        if h.get("is_admission_charge") and (h.get("program") or PROGRAM) == PROGRAM
    ]
    active = [h for h in heads if h.get("is_active", True)]
    chosen = (active or heads)
    if not chosen:
        sys.exit("No fee head is marked as the admission charge. Create one under Fee heads first.")
    return chosen[0]


def school_students() -> list[dict]:
    out = []
    for user in firestore_users.query_documents("role", "==", "STUDENT"):
        programs = user.get("programs") or [PROGRAM]
        if PROGRAM in programs and user.get("is_active", True):
            out.append(user)
    return out


def read_csv(path: str | None) -> dict:
    """Overrides keyed by student id and by admission number, whichever the row gives."""
    if not path:
        return {}
    overrides = {}
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            for key in ("student_id", "admission_number"):
                if row.get(key):
                    overrides[row[key]] = row
    return overrides


def plan(args) -> tuple[list[dict], list[dict], dict]:
    head = admission_head()
    categories = {str(c["id"]): c for c in firestore_admission_categories.list_all()}
    years = {str(y["id"]): y for y in firestore_academic_years.list_all()}
    overrides = read_csv(args.csv)
    already = {
        str(r["student_id"])
        for r in receipts.list_receipts(PROGRAM, fee_head_id=head["id"], source=receipts.OPENING_BALANCE)
    }
    default_amount = money(args.amount if args.amount is not None else head.get("default_amount"))

    todo, skipped = [], []
    for student in school_students():
        sid = str(student["id"])
        category = categories.get(str(student.get("admission_category_id") or ""))
        if not args.all and not (category and category.get("waives_admission_charge")):
            skipped.append({"student": student, "why": "category does not say the fee was already paid"})
            continue
        if sid in already:
            skipped.append({"student": student, "why": "already has an opening-balance receipt"})
            continue
        year_id = admission_year_of(student, PROGRAM)
        year = years.get(str(year_id)) if year_id else None
        if not year:
            skipped.append({"student": student, "why": "not assigned to any academic year"})
            continue

        override = overrides.get(sid) or overrides.get(str(student.get("admission_number") or ""), {})
        if override.get("paid_on"):
            paid_on, basis = _as_date(override["paid_on"]), "the date from the CSV"
        elif _as_date(student.get("admission_date")):
            paid_on, basis = _as_date(student.get("admission_date")), "the admission date on the profile"
        elif args.paid_on:
            paid_on, basis = args.paid_on, "the date given when it was entered"
        else:
            paid_on, basis = _as_date(year.get("start_date")), "the start of the admission year (the exact date was not recorded)"
        if paid_on is None:
            skipped.append({"student": student, "why": "no usable date"})
            continue

        amount = money(override["amount"]) if override.get("amount") else default_amount
        method = (override.get("method") or PaymentMethod.OTHER.value).upper()
        if method not in {m.value for m in PaymentMethod}:
            skipped.append({"student": student, "why": f"unknown method {method!r} in the CSV"})
            continue

        todo.append({
            "student": student,
            "category": (category or {}).get("name"),
            "year": year,
            "paid_on": paid_on,
            "basis": basis,
            "amount": amount,
            "method": method,
            "reference": override.get("reference") or "OPENING",
        })
    return todo, skipped, head


def apply(todo: list[dict], head: dict) -> int:
    written = 0
    for item in todo:
        payload = FeeReceiptCreate(
            student_id=int(item["student"]["id"]),
            fee_head_id=int(head["id"]),
            amount=item["amount"],
            paid_at=item["paid_on"],
            method=item["method"],
            reference=item["reference"],
            note=(
                "Admission fee paid at admission, before this system. Entered as an opening "
                f"balance; the date is {item['basis']}."
            ),
            academic_year_id=int(item["year"]["id"]),
        )
        receipts.record_receipt(payload, actor_id=None, program=PROGRAM)
        written += 1
    return written


def undo(args) -> None:
    head = admission_head()
    existing = receipts.list_receipts(PROGRAM, fee_head_id=head["id"], source=receipts.OPENING_BALANCE)
    print(f"{len(existing)} opening-balance receipt(s) for {head.get('name')}.")
    for r in existing:
        student = firestore_users.get_document(str(r.get("student_id"))) or {}
        print(f"  #{r['id']}  {student.get('full_name') or r.get('student_id'):<32} {r.get('paid_at', '')[:10]}  {r.get('currency')} {r.get('amount')}")
    if not args.apply:
        print("\nDry run: nothing removed. Add --apply to remove them.")
        return
    for r in existing:
        receipts.delete_receipt(r)
    print(f"Removed {len(existing)}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Write (or with --undo, remove) the receipts. Without it, the plan is printed and nothing changes.")
    parser.add_argument("--undo", action="store_true", help="Remove the opening-balance admission receipts instead of creating them.")
    parser.add_argument("--paid-on", type=date.fromisoformat, help="Date for students with no admission date and no CSV row (YYYY-MM-DD).")
    parser.add_argument("--amount", type=float, help="Override the admission-fee head's amount.")
    parser.add_argument("--csv", help="Per-student dates, amounts, methods and references.")
    parser.add_argument("--all", action="store_true", help="Every school student, not only those whose category waives the charge.")
    args = parser.parse_args()

    initialize_firebase()

    if args.undo:
        undo(args)
        return

    todo, skipped, head = plan(args)
    print(f"Admission fee head: {head.get('name')} (#{head.get('id')}), default {head.get('default_amount')}\n")
    print(f"{len(todo)} receipt(s) to record:")
    for item in todo:
        s = item["student"]
        print(
            f"  {s.get('full_name') or s['id']:<32} {s.get('admission_number') or '':<12} "
            f"{item['year'].get('name'):<10} {item['paid_on']}  {item['amount']:>10.2f}  {item['method']:<8} "
            f"date = {item['basis']}"
        )
    if skipped:
        print(f"\n{len(skipped)} skipped:")
        for item in skipped:
            s = item["student"]
            print(f"  {s.get('full_name') or s['id']:<32} {item['why']}")

    if not args.apply:
        print("\nDry run: nothing written. Add --apply to record them.")
        return
    written = apply(todo, head)
    print(f"\nRecorded {written} receipt(s). They appear in Fees & invoices > Reports > Collections over a period that includes their dates.")


if __name__ == "__main__":
    main()
