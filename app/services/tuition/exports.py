"""
CSV exports for the tuition module.

Every figure this system produces is derived from something - an invoice line from a count of
conducted classes, a count from individual sessions - and an administrator eventually needs
to take that somewhere else: an accountant, a spreadsheet, a parent asking which eight
classes they are paying for. So the exports here are built around one rule:

    **Export the workings, not just the totals.**

A CSV of invoice totals is a report you have to trust. A CSV that carries the session count
beside the amount, and a second export that lists the sessions themselves with their dates
and attendance, is a report you can *check*. The difference is what turns a billing dispute
from an argument into a lookup.

CSV rather than XLSX deliberately: it opens in Excel, Sheets and Numbers alike, it needs no
dependency, and it survives being emailed, diffed and imported by accounting software that
would choke on anything richer.
"""

import csv
import io
import logging
from datetime import date, datetime

from app.core.firebase import (
    firestore_subjects, firestore_tuition_sessions, firestore_users,
)
from app.services.tuition.common import parse_date, to_utc, user_timezone
from app.services.tuition.sessions import filter_sessions, is_billable

logger = logging.getLogger("tuition.exports")


def _write(rows: list[list], header: list[str]) -> str:
    """
    Renders rows as CSV text.

    `\\r\\n` line endings and QUOTE_MINIMAL are what RFC 4180 specifies and what Excel expects;
    a file written with bare newlines opens as one long row in some Windows configurations.
    """
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def _cell(value):
    """
    Flattens a value for a spreadsheet cell.

    Booleans become Yes/No because TRUE/FALSE in a CSV is re-interpreted differently by every
    spreadsheet that opens it, and None becomes an empty cell rather than the text "None",
    which is the single most common way an export looks broken to whoever receives it.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def filename(stem: str, *parts) -> str:
    """A predictable, sortable download name: tuition-invoices_2026-09-01_2026-09-30.csv."""
    pieces = [str(p) for p in parts if p]
    return "_".join([stem, *pieces]) + ".csv"


# ---------------------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------------------

INVOICE_SUMMARY_HEADER = [
    "Invoice ID", "Student ID", "Student", "Admission Number", "Period Start", "Period End",
    "Status", "Currency", "Subtotal", "Discount", "Tax", "Total", "Paid", "Outstanding",
    "Classes Billed", "Classes Conducted", "Classes Attended", "Classes Missed",
    "Due Date", "Issued At", "Generated At",
]


def _totals(invoice: dict) -> tuple[float, float, float]:
    total = float(invoice.get("total_amount") or 0)
    paid = float(invoice.get("amount_paid") or 0)
    return total, paid, round(total - paid, 2)


def _counts(invoice: dict) -> dict:
    """Class counts summed across an invoice's lines, for the one-row-per-invoice view."""
    keys = ("sessions_billable", "sessions_conducted", "sessions_attended", "sessions_missed")
    return {
        key: sum(int(line.get(key) or 0) for line in invoice.get("line_items") or [])
        for key in keys
    }


def invoices_csv(invoices: list[dict]) -> str:
    """One row per invoice: the ledger view, for reconciling against payments received."""
    rows = []
    for invoice in invoices:
        total, paid, outstanding = _totals(invoice)
        counts = _counts(invoice)
        rows.append([_cell(v) for v in [
            invoice.get("id"), invoice.get("student_id"), invoice.get("student_name"),
            invoice.get("admission_number"), invoice.get("period_start"),
            invoice.get("period_end"), invoice.get("status"), invoice.get("currency"),
            invoice.get("subtotal"), invoice.get("discount_amount"), invoice.get("tax_amount"),
            total, paid, outstanding,
            counts["sessions_billable"], counts["sessions_conducted"],
            counts["sessions_attended"], counts["sessions_missed"],
            invoice.get("due_date"), invoice.get("issued_at"), invoice.get("created_at"),
        ]])
    return _write(rows, INVOICE_SUMMARY_HEADER)


INVOICE_LINE_HEADER = [
    "Invoice ID", "Student ID", "Student", "Admission Number", "Period Start", "Period End",
    "Invoice Status", "Subject", "Teacher", "Fee Plan", "Basis", "Unit Amount", "Quantity",
    "Unit", "Classes Conducted", "Classes Billed", "Classes Attended", "Classes Missed",
    "Teacher No-Show", "Taught Minutes", "Currency", "Line Amount",
]


def invoice_lines_csv(invoices: list[dict]) -> str:
    """
    One row per subject per invoice - the workings behind every total.

    This is the export an accountant actually wants and the one a parent's query is answered
    from: it says which subject, which teacher, how many classes, at what rate, and how many
    of those classes the student actually attended.
    """
    rows = []
    for invoice in invoices:
        for line in invoice.get("line_items") or []:
            rows.append([_cell(v) for v in [
                invoice.get("id"), invoice.get("student_id"), invoice.get("student_name"),
                invoice.get("admission_number"), invoice.get("period_start"),
                invoice.get("period_end"), invoice.get("status"),
                line.get("subject_name"), line.get("teacher_name"), line.get("fee_plan_name"),
                line.get("basis"), line.get("unit_amount"), line.get("quantity"),
                line.get("unit_label"), line.get("sessions_conducted"),
                line.get("sessions_billable"), line.get("sessions_attended"),
                line.get("sessions_missed"), line.get("teacher_no_show"),
                line.get("taught_minutes"), invoice.get("currency"), line.get("amount"),
            ]])
    return _write(rows, INVOICE_LINE_HEADER)


PAYMENT_HEADER = [
    "Invoice ID", "Student ID", "Student", "Period Start", "Period End", "Invoice Total",
    "Currency", "Payment Amount", "Paid At", "Method", "Reference", "Recorded By",
]


def payments_csv(invoices: list[dict]) -> str:
    """One row per payment received. The receipts side of the ledger."""
    rows = []
    for invoice in invoices:
        for payment in invoice.get("payments") or []:
            rows.append([_cell(v) for v in [
                invoice.get("id"), invoice.get("student_id"), invoice.get("student_name"),
                invoice.get("period_start"), invoice.get("period_end"),
                invoice.get("total_amount"), invoice.get("currency"),
                payment.get("amount"), payment.get("paid_at"), payment.get("method"),
                payment.get("reference"), payment.get("recorded_by"),
            ]])
    return _write(rows, PAYMENT_HEADER)


# ---------------------------------------------------------------------------------------
# The sessions behind a bill
# ---------------------------------------------------------------------------------------

SESSION_HEADER = [
    "Session ID", "Date", "Start (local)", "End (local)", "Timezone", "Student",
    "Admission Number", "Teacher", "Subject", "Status", "Attendance", "Scheduled Minutes",
    "Taught Minutes", "Teacher Late (min)", "Student Late (min)", "Extension (min)",
    "Billable", "Enrollment ID", "Remarks",
]


def sessions_csv(sessions: list[dict], viewer=None) -> str:
    """
    One row per class - the evidence every count on an invoice was derived from.

    Times are rendered in the reader's own zone, because an administrator checking "was there
    a class on the 14th?" against a parent's recollection needs the hour that parent saw, not
    UTC. `Billable` is the flag that decides whether the row reached the invoice at all, so a
    row that is present here but absent from the bill explains itself.
    """
    zone = user_timezone(viewer) if viewer is not None else None
    people: dict = {}
    subjects: dict = {}

    def person(user_id):
        if user_id not in people:
            people[user_id] = firestore_users.get_document(str(user_id)) or {}
        return people[user_id]

    def subject(subject_id):
        if subject_id not in subjects:
            subjects[subject_id] = firestore_subjects.get_document(str(subject_id)) or {}
        return subjects[subject_id]

    def local(value):
        moment = to_utc(value)
        if moment is None:
            return ""
        return (moment.astimezone(zone) if zone else moment).strftime("%Y-%m-%d %H:%M")

    rows = []
    for session in sessions:
        student = person(session.get("student_id"))
        teacher = person(session.get("teacher_id"))
        rows.append([_cell(v) for v in [
            session.get("id"), session.get("session_date"),
            local(session.get("scheduled_start_at")),
            local(session.get("effective_end_at") or session.get("scheduled_end_at")),
            str(zone) if zone else "UTC",
            student.get("full_name"), student.get("admission_number"),
            teacher.get("full_name"), subject(session.get("subject_id")).get("name"),
            session.get("status"), session.get("attendance_status"),
            session.get("duration_minutes"), session.get("actual_duration_minutes"),
            session.get("teacher_late_minutes"), session.get("student_late_minutes"),
            session.get("extension_minutes"), is_billable(session),
            session.get("enrollment_id"), session.get("attendance_remarks"),
        ]])
    return _write(rows, SESSION_HEADER)


def sessions_for_invoice(invoice: dict) -> list[dict]:
    """
    Every class that fell inside an invoice's period, for the arrangements it bills.

    Read back from the sessions rather than stored on the invoice: the invoice keeps the
    *counts* it was priced from, and this reconstructs the rows behind them. Storing a copy of
    every session on every invoice would double the data and give two places for the truth to
    live.
    """
    start = parse_date(invoice.get("period_start"))
    end = parse_date(invoice.get("period_end"))
    enrollment_ids = {
        str(line.get("enrollment_id")) for line in invoice.get("line_items") or []
    }
    if not enrollment_ids:
        return []

    collected: list[dict] = []
    for enrollment_id in enrollment_ids:
        collected.extend(
            filter_sessions(
                firestore_tuition_sessions.query_documents(
                    "enrollment_id", "==", _numeric(enrollment_id)
                ),
                start, end,
            )
        )
    return sorted(collected, key=lambda s: str(s.get("scheduled_start_at") or ""))


def _numeric(value):
    """Enrollment ids are stored numerically; a string from a line item has to match."""
    text = str(value)
    return int(text) if text.isdigit() else value


# ---------------------------------------------------------------------------------------
# Attendance reports
# ---------------------------------------------------------------------------------------

def report_rows_csv(rows: list[dict], first_column: str, first_key: str,
                    id_key: str, extra: list[tuple[str, str]] | None = None) -> str:
    """
    Renders a programme, student or teacher report as CSV.

    One helper for all three because they are the same shape - an identity plus the counts
    from `reports.summarize` - and three near-identical writers would drift the moment a
    count was added to one of them.
    """
    extra = extra or []
    header = [
        "ID", first_column, *[label for label, _ in extra],
        "Total Sessions", "Conducted", "Attended", "Late", "Missed", "Cancelled",
        "Teacher No-Show", "Upcoming", "Billable", "Attendance %",
        "Scheduled Minutes", "Taught Minutes",
    ]
    data = []
    for row in rows:
        data.append([_cell(v) for v in [
            row.get(id_key), row.get(first_key),
            *[row.get(key) for _, key in extra],
            row.get("total_sessions"), row.get("conducted"), row.get("attended"),
            row.get("late"), row.get("missed"), row.get("cancelled"),
            row.get("teacher_no_show"), row.get("upcoming"), row.get("billable_sessions"),
            row.get("attendance_percentage"), row.get("scheduled_minutes"),
            row.get("taught_minutes"),
        ]])
    return _write(data, header)


def billing_summary(invoices: list[dict], currency: str) -> dict:
    """
    Headline figures for the billing screen, over whatever set of invoices it is showing.

    Computed from the same list the screen displays rather than from a separate query, so the
    totals at the top can never disagree with the rows underneath - which is the way a
    billing summary loses an administrator's trust fastest.
    """
    live = [i for i in invoices if i.get("status") != "CANCELLED"]
    billed = round(sum(float(i.get("total_amount") or 0) for i in live), 2)
    collected = round(sum(float(i.get("amount_paid") or 0) for i in live), 2)

    by_status: dict[str, dict] = {}
    for invoice in invoices:
        bucket = by_status.setdefault(
            str(invoice.get("status")), {"count": 0, "total": 0.0, "paid": 0.0}
        )
        bucket["count"] += 1
        bucket["total"] = round(bucket["total"] + float(invoice.get("total_amount") or 0), 2)
        bucket["paid"] = round(bucket["paid"] + float(invoice.get("amount_paid") or 0), 2)

    overdue = _overdue(live)
    classes = {
        key: sum(_counts(i)[key] for i in live)
        for key in ("sessions_billable", "sessions_conducted", "sessions_attended",
                    "sessions_missed")
    }

    return {
        "currency": currency,
        "invoice_count": len(invoices),
        "total_billed": billed,
        "total_collected": collected,
        "outstanding": round(billed - collected, 2),
        "overdue_count": len(overdue),
        "overdue_amount": round(
            sum(float(i.get("total_amount") or 0) - float(i.get("amount_paid") or 0)
                for i in overdue), 2
        ),
        "classes_billed": classes["sessions_billable"],
        "classes_conducted": classes["sessions_conducted"],
        "classes_attended": classes["sessions_attended"],
        "classes_missed": classes["sessions_missed"],
        "by_status": by_status,
    }


def _overdue(invoices: list[dict]) -> list[dict]:
    """
    Issued, past its due date, and not settled.

    A draft is never overdue however old it is - it has not been sent to anybody, so nobody
    is late paying it. Getting that wrong would put unbilled work on an aged-debt report.
    """
    today = datetime.utcnow().date()
    late = []
    for invoice in invoices:
        if invoice.get("status") in {"DRAFT", "PAID", "CANCELLED"}:
            continue
        due = parse_date(invoice.get("due_date"))
        if due and due < today:
            late.append(invoice)
    return late


def student_billing_history(student_id: int, invoices: list[dict]) -> dict:
    """
    One student's whole billing history, for the account view an administrator opens when a
    parent calls.

    Ordered newest-first, with a running outstanding figure, because the question being asked
    is almost always "what do they owe right now, and what for?".
    """
    student = firestore_users.get_document(str(student_id)) or {}
    mine = sorted(
        [i for i in invoices if i.get("student_id") == student_id],
        key=lambda i: str(i.get("period_start") or ""), reverse=True,
    )
    return {
        "student_id": student_id,
        "student_name": student.get("full_name"),
        "admission_number": student.get("admission_number"),
        "email": student.get("email"),
        "phone": student.get("phone"),
        "guardian_name": student.get("guardian_name"),
        "guardian_phone": student.get("guardian_phone"),
        "invoice_count": len(mine),
        "invoices": mine,
    }
