"""
Receipts for money that has no invoice on this system.

A school does not start its books the day it starts its software. Every student on the roll
when the fee module went live had already paid an admission fee - at a counter, into a
ledger, years ago in some cases - and the office still wants that money in the collections
report, because "admission fees collected" is a real figure whether or not an invoice on
this system produced it.

An invoice cannot hold it: an invoice is what the rules say a student owes for a year, and
there is exactly one per student per year, so a "paid admission fee" invoice for 2025-26
would BE the year's invoice and the tuition would never get billed. So a receipt stands on
its own: one fee head, one amount, one date, one student, and a `source` saying where it
came from. `OPENING_BALANCE` is the one that exists today - money received before the
system, entered afterwards. The collections report reads receipts alongside invoice
payments and flags them, so a reconciliation can tell the two apart.

A receipt never changes what a student owes. The rules already do that: a category that
waives the admission charge is how "already paid" is expressed on the bill, and the receipt
is how it is expressed in the cash book. The two agree because both were set by the same
decision, not because one is computed from the other.
"""

from datetime import date, datetime

from app.core.enums import Program
from app.core.firebase import firestore_fee_receipts, firestore_users, require_document
from app.services.finance import admission_year_of, money, require_head
from app.services.tuition.settings_store import currency_settings

OPENING_BALANCE = "OPENING_BALANCE"


def _now() -> str:
    return datetime.utcnow().isoformat()


def _as_datetime(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return datetime.fromisoformat(str(value))


def record_receipt(payload, actor_id: int | None = None,
                   program: str = Program.LMS.value,
                   source: str = OPENING_BALANCE) -> dict:
    """
    Writes one receipt. The fee head's name and admission flag are copied onto it, so the
    receipt still reads correctly if the head is renamed or retired later - a cash book
    entry describes what was received at the time, not what the price list says now.
    """
    student = require_document(firestore_users, payload.student_id, "Student")
    head = require_head(payload.fee_head_id)
    year_id = payload.academic_year_id
    if year_id in (None, ""):
        year_id = admission_year_of(student, program)

    receipt_id = firestore_fee_receipts.get_next_numeric_id()
    document = {
        "id": receipt_id,
        "program": program,
        "student_id": int(student["id"]),
        "academic_year_id": int(year_id) if year_id not in (None, "") else None,
        "fee_head_id": int(head["id"]),
        "head_name": head.get("name"),
        "is_admission_charge": bool(head.get("is_admission_charge")),
        "amount": money(payload.amount),
        "currency": currency_settings(program)["base_currency"],
        "paid_at": _as_datetime(payload.paid_at).isoformat(),
        "method": getattr(payload.method, "value", payload.method),
        "reference": payload.reference,
        "note": payload.note,
        "source": source,
        "recorded_by": actor_id,
        "recorded_at": _now(),
    }
    firestore_fee_receipts.create_document(str(receipt_id), document)
    return document


def list_receipts(program: str = Program.LMS.value, student_id=None,
                  academic_year_id=None, fee_head_id=None,
                  source: str | None = None) -> list[dict]:
    """Newest first. Filters are all optional; a bare call is the whole book."""
    if student_id is not None:
        documents = firestore_fee_receipts.query_documents("student_id", "==", int(student_id))
    else:
        documents = firestore_fee_receipts.list_all()
    out = []
    for doc in documents:
        if (doc.get("program") or Program.LMS.value) != str(program).upper():
            continue
        if academic_year_id is not None and str(doc.get("academic_year_id")) != str(academic_year_id):
            continue
        if fee_head_id is not None and str(doc.get("fee_head_id")) != str(fee_head_id):
            continue
        if source is not None and (doc.get("source") or OPENING_BALANCE) != source:
            continue
        out.append(doc)
    out.sort(key=lambda d: str(d.get("paid_at")), reverse=True)
    return out


def require_receipt(receipt_id) -> dict:
    return require_document(firestore_fee_receipts, receipt_id, "Receipt")


def delete_receipt(receipt: dict) -> None:
    """
    Removes a receipt outright. There is no cancelled state: a receipt is a statement that
    money arrived, and a wrong one is a mistake to erase, not a transaction to reverse.
    """
    firestore_fee_receipts.delete_document(str(receipt["id"]))


def present_receipt(receipt: dict) -> dict:
    student = firestore_users.get_document(str(receipt.get("student_id"))) or {}
    return {
        **receipt,
        "student_name": student.get("full_name"),
        "admission_number": student.get("admission_number"),
        "source": receipt.get("source") or OPENING_BALANCE,
    }
