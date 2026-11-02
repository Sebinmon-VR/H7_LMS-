"""
Billing many students at once, from Fees & Invoices -> Bulk billing.

Three things can be billed to everyone selected, each as a numbered bill of their own:

  * ISSUE - their fees for the year, for students not billed yet. The office chooses which
    fees go on (Admission + Term 1, say); the rest are held back, exactly as unticking them
    at the counter would.
  * LATER - fees that were held back (Term 2), for students who still have them waiting.
  * EXTRA - the same extra item (Annual day, Books) for students already billed for the year.

Every student is billed by the same functions the fee counter calls, one after another, so
a bill made here is indistinguishable from one made at the counter. A preview runs the same
checks without billing anyone: what each student would be billed, and who would be skipped
and why. Both run as background jobs, because a whole school takes a while.
"""

import logging

from fastapi import HTTPException

from app.core import mailer
from app.core.enums import Program
from app.core.firebase import firestore_classes, firestore_fee_invoices, firestore_users
from app.services import admissions as admission_service
from app.services import billing, finance
from app.services.finance import money

logger = logging.getLogger("bulk_billing")

ISSUE, LATER, EXTRA = "ISSUE", "LATER", "EXTRA"
# What a row ends as: READY / SKIP in a preview; BILLED / SKIPPED / FAILED when run.
READY, SKIP, BILLED, SKIPPED, FAILED = "READY", "SKIP", "BILLED", "SKIPPED", "FAILED"


def _student_row(student_id: int, student: dict) -> dict:
    enrolled_class, _ = admission_service._class_of_student(student_id)
    class_doc = firestore_classes.get_document(str(enrolled_class)) if enrolled_class else None
    return {
        "student_id": student_id,
        "student_name": student.get("full_name") or f"Student {student_id}",
        "admission_number": student.get("admission_number"),
        "class_name": (class_doc or {}).get("name"),
        "lines": [],
        "amount": 0.0,
        "bill_number": None,
        "emailed": [],
        "detail": None,
    }


def _plan(student_id: int, student: dict, academic_year_id, action: str, labels: list[str],
          items: list[dict], program: str) -> tuple[dict, dict | None]:
    """
    What billing this student would do, without doing it: the row to show and, when it can
    go ahead, the student's account (None for ISSUE, which builds one).
    """
    row = _student_row(student_id, student)
    if not student:
        return {**row, "outcome": SKIP, "detail": "This student could not be found."}, None
    account = firestore_fee_invoices.get_document(billing.invoice_id_for(student_id, academic_year_id))
    issued = bool(account) and account.get("status") in billing.FROZEN_STATUSES

    if action == ISSUE:
        if issued:
            return {**row, "outcome": SKIP,
                    "detail": f"Already billed for the year ({account.get('invoice_number')})."}, None
        breakdown = finance.compute_breakdown(student, academic_year_id, program)
        lines = [{"label": i.get("label"), "amount": money(i.get("amount")), "due_date": i.get("due_date")}
                 for i in breakdown.get("instalments") or [] if money(i.get("amount")) > 0]
        if not lines:
            return {**row, "outcome": SKIP,
                    "detail": "No fee is set for this student. Give them an admission type and this "
                              "year's session under Admissions."}, None
        return _chosen(row, lines, labels, "None of the chosen fees apply to this student."), None

    if not issued:
        return {**row, "outcome": SKIP, "detail": "Not billed for the year yet. Bill their fees for the year first."}, None

    if action == LATER:
        waiting = list(account.get("left_off") or [])
        if not waiting:
            return {**row, "outcome": SKIP, "detail": "No fees are held back for this student."}, None
        breakdown = finance.compute_breakdown(student, academic_year_id, program,
                                              currency=account.get("currency"))
        lines = [{"label": i.get("label"), "amount": money(i.get("amount")), "due_date": i.get("due_date")}
                 for i in breakdown.get("instalments") or [] if i.get("label") in waiting]
        return _chosen(row, lines, labels, "None of the chosen fees are held back for this student."), account

    lines = [{"label": i["name"], "amount": money(i["amount"]), "due_date": i.get("due_date")} for i in items]
    return {**row, "outcome": READY, "lines": lines, "amount": money(sum(l["amount"] for l in lines))}, account


def _chosen(row: dict, lines: list[dict], labels: list[str], none_message: str) -> dict:
    """
    Every line the student could be billed, marked with whether it was chosen. The screen
    re-totals as fees are ticked and unticked, so the preview carries them all.
    """
    picked = [l for l in lines if not labels or l["label"] in labels]
    marked = [{**l, "chosen": l in picked} for l in lines]
    if not picked:
        return {**row, "outcome": SKIP, "lines": marked, "detail": none_message}
    return {**row, "outcome": READY, "lines": marked, "amount": money(sum(l["amount"] for l in picked))}


def _bill(row: dict, student_id: int, academic_year_id, account: dict | None, action: str,
          labels: list[str], items: list[dict], actor_id, program: str) -> dict:
    """Bills one student as the counter would. Returns the account as it now stands."""
    if action == ISSUE:
        draft = billing.generate_invoice(student_id, academic_year_id, actor_id, program)
        wanted = {l["label"] for l in row["lines"] if l.get("chosen", True)}
        exclude = [i.get("label") for i in draft.get("instalments") or [] if i.get("label") not in wanted]
        return billing.issue_invoice(draft, actor_id, program, exclude=exclude)
    if action == LATER:
        wanted = [l["label"] for l in row["lines"] if l.get("chosen", True)]
        return billing.issue_left_off(account, wanted, actor_id, program=program)
    return billing.add_extra_items(account, items, actor_id)


def _email(student_id: int, student: dict, account: dict, bill_number: str) -> list[str]:
    if not mailer.is_configured():
        return []
    view = billing.bills_view(account, account.get("instalments") or [])
    bill = next((b for b in view if b["number"] == bill_number), None)
    if bill is None:
        return []
    year = admission_service.firestore_academic_years.get_document(
        str(account.get("academic_year_id"))) or {}
    letter = {**bill, "student_name": student.get("full_name"), "currency": account.get("currency"),
              "academic_year_name": year.get("name")}
    sent = []
    for person in billing.receipt_recipients({**student, "id": student_id}):
        url = mailer.absolute_link(person["view_path"] or "/")
        if mailer.send_fee_bill_email(person["email"], person["name"], letter, url):
            sent.append(person["email"])
    return sent


def checked(action: str, labels: list[str] | None, items: list[dict] | None,
            dry_run: bool) -> tuple[str, list[str], list[dict]]:
    """The request made usable, or a 400 saying what is missing - before any job starts."""
    action = str(action).upper()
    if action not in (ISSUE, LATER, EXTRA):
        raise HTTPException(status_code=400, detail="Choose what to bill.")
    labels = [str(l) for l in labels or [] if l]
    if action == LATER and not labels and not dry_run:
        raise HTTPException(status_code=400, detail="Tick the held-back fees to bill.")
    items = [{"name": str(i["name"]).strip(), "amount": money(i["amount"]), "due_date": i.get("due_date")}
             for i in items or [] if str(i.get("name") or "").strip() and money(i.get("amount")) > 0]
    if action == EXTRA and not items:
        raise HTTPException(status_code=400, detail="Give the item a name and an amount.")
    return action, labels, items


def run(student_ids: list[int], academic_year_id, action: str, labels: list[str] | None = None,
        items: list[dict] | None = None, email: bool = False, dry_run: bool = True,
        actor_id: int | None = None, program: str = Program.LMS.value, progress=None) -> dict:
    """
    Previews (`dry_run`) or bills every student in `student_ids`, in order, reporting
    progress as it goes. A student who cannot be billed is skipped with the reason; one that
    fails is reported and the rest carry on.
    """
    action, labels, items = checked(action, labels, items, dry_run)
    ids = list(dict.fromkeys(int(s) for s in student_ids))
    rows = []
    for n, student_id in enumerate(ids, start=1):
        student = firestore_users.get_document(str(student_id)) or {}
        if progress:
            progress(int((n - 1) * 100 / max(len(ids), 1)),
                     f"{'Checking' if dry_run else 'Billing'} {n} of {len(ids)}: {student.get('full_name') or student_id}")
        try:
            row, account = _plan(student_id, student, academic_year_id, action, labels, items, program)
            if dry_run or row["outcome"] == SKIP:
                rows.append(row if dry_run else {**row, "outcome": SKIPPED})
                continue
            updated = _bill(row, student_id, academic_year_id, account, action, labels, items, actor_id, program)
            number = (updated.get("bills") or [{}])[-1].get("number") or updated.get("invoice_number")
            row = {**row, "outcome": BILLED, "bill_number": number}
            if email:
                row["emailed"] = _email(student_id, student, updated, number)
            rows.append(row)
        except HTTPException as exc:
            rows.append({**_student_row(student_id, student), "outcome": SKIP if dry_run else FAILED,
                         "detail": str(exc.detail)})
        except Exception as exc:  # one bad record must not stop the rest of the class
            logger.exception("Bulk billing failed for student %s", student_id)
            rows.append({**_student_row(student_id, student), "outcome": SKIP if dry_run else FAILED,
                         "detail": f"Could not be billed: {exc}"})

    counted = {}
    for row in rows:
        counted[row["outcome"]] = counted.get(row["outcome"], 0) + 1
    go = READY if dry_run else BILLED
    available = list(dict.fromkeys(l["label"] for r in rows for l in r.get("lines") or []))
    if progress:
        progress(100, "Preview ready" if dry_run else f"Billed {counted.get(BILLED, 0)} of {len(ids)}")
    return {
        "action": action,
        "dry_run": dry_run,
        "academic_year_id": academic_year_id,
        # Every fee line seen across the students, for the fee tick boxes on the screen.
        "labels": available,
        "summary": {
            "students": len(ids),
            "counts": counted,
            "amount": money(sum(r["amount"] for r in rows if r["outcome"] == go)),
            "emailed": sum(len(r.get("emailed") or []) for r in rows),
            "mail_configured": mailer.is_configured(),
        },
        "rows": rows,
    }
