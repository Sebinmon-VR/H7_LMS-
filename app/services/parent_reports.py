"""
Weekly and monthly progress digests, emailed to parents.

One report per child per period, built from what already exists - attendance, marks,
homework, fees - rather than from anything stored for the purpose. That is the whole design:
a digest is a *view*, so it can be regenerated for any past period, previewed before it is
sent, and corrected by fixing the underlying record rather than the report.

**Sending is logged, and the log is what prevents duplicates.** `report_log` carries one
document per (student, period, channel) with a derived id, claimed before the mail goes out.
A sweep that runs twice, or two workers that both wake up on Sunday night, send one email -
which matters more here than almost anywhere else in this codebase, because the recipient is
a parent and the failure mode is visible to them.

**A report with nothing in it is not sent.** A family whose child joined last week, or whose
period contains no school days, gets silence rather than a digest of zeros. An email that
says nothing trains people to stop opening the ones that say something.

Recipients come from `families.report_recipients`, which falls back to the guardian email on
the student's own profile when no parent account exists - most families will never create a
login, and a report nobody receives is not a report.
"""

import logging
from datetime import date, datetime, timedelta

from app.core.enums import AttendanceStatus, Program
from app.core.firebase import (
    firestore_attendance, firestore_classes, firestore_exam_submissions, firestore_exams,
    firestore_homework, firestore_homework_submissions, firestore_reminder_log,
    firestore_student_enrollments, firestore_subjects, firestore_users,
)
from app.core.mailer import is_configured as mail_is_configured, send_email
from app.services import families as family_service

logger = logging.getLogger("parent_reports")

WEEKLY = "WEEKLY"
MONTHLY = "MONTHLY"


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def period_bounds(period: str, on: date | None = None) -> tuple[date, date]:
    """
    The last complete week or month before `on`.

    Deliberately the *last complete* one, not the current partial one. A Monday digest
    covering the week that started that morning reports nothing; parents want last week.
    """
    today = on or date.today()

    if period == MONTHLY:
        first_of_this = today.replace(day=1)
        end = first_of_this - timedelta(days=1)
        return end.replace(day=1), end

    # Weekly: the Monday-to-Sunday block that has just finished.
    last_sunday = today - timedelta(days=today.isoweekday() % 7 or 7)
    return last_sunday - timedelta(days=6), last_sunday


def report_key(student_id, period: str, start: date, channel: str = "EMAIL") -> str:
    """Derived id for the send log; see the module docstring."""
    return f"parent_report:{student_id}:{period}:{start.isoformat()}:{channel}"


# ---------------------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------------------

def _attendance_summary(student_id, start: date, end: date) -> dict:
    records = [
        r for r in firestore_attendance.query_documents("student_id", "==", int(student_id))
        if (d := _as_date(r.get("date"))) and start <= d <= end
    ]
    counts = {status.value: 0 for status in AttendanceStatus}
    for record in records:
        status = record.get("status")
        if status in counts:
            counts[status] += 1

    total = len(records)
    present = counts[AttendanceStatus.PRESENT.value] + counts[AttendanceStatus.LATE.value]
    return {
        "total_sessions": total,
        "present": counts[AttendanceStatus.PRESENT.value],
        "absent": counts[AttendanceStatus.ABSENT.value],
        "late": counts[AttendanceStatus.LATE.value],
        "excused": counts[AttendanceStatus.EXCUSED.value],
        "percent": round(present * 100.0 / total, 1) if total else None,
    }


def _exam_summary(student_id, start: date, end: date) -> dict:
    submissions = firestore_exam_submissions.query_documents(
        "student_id", "==", int(student_id)
    )
    rows = []
    for submission in submissions:
        exam = firestore_exams.get_document(str(submission.get("exam_id")))
        if not exam:
            continue
        when = _as_date(exam.get("starts_at")) or _as_date(exam.get("created_at"))
        if not when or not (start <= when <= end):
            continue

        rows.append({
            "exam_title": exam.get("title"),
            "subject_name": (
                firestore_subjects.get_document(str(exam.get("subject_id"))) or {}
            ).get("name"),
            "date": when.isoformat(),
            "status": submission.get("status"),
            "marks": submission.get("marks_awarded"),
            "max_marks": exam.get("total_marks"),
            "grade": submission.get("grade"),
        })

    graded = [r for r in rows if r["marks"] is not None and r["max_marks"]]
    average = (
        round(sum(r["marks"] / float(r["max_marks"]) for r in graded) * 100.0 / len(graded), 1)
        if graded else None
    )
    return {
        "exams": sorted(rows, key=lambda r: r["date"]),
        "count": len(rows),
        "missed": sum(1 for r in rows if r["status"] == "MISSED"),
        "average_percent": average,
    }


def _homework_summary(student_id, start: date, end: date) -> dict:
    class_ids = {
        int(e["class_id"]) for e in
        firestore_student_enrollments.query_documents("student_id", "==", int(student_id))
        if e.get("class_id") is not None
    }
    assignments = [
        a for a in firestore_homework.list_all()
        if int(a.get("class_id", -1)) in class_ids
        and (d := _as_date(a.get("due_date"))) and start <= d <= end
    ]

    submitted = late = missed = 0
    for assignment in assignments:
        record = firestore_homework_submissions.get_document(
            f"{assignment['id']}:{student_id}"
        )
        if record and record.get("submitted_at"):
            submitted += 1
            if record.get("is_late"):
                late += 1
        else:
            missed += 1

    return {
        "set": len(assignments),
        "submitted": submitted,
        "late": late,
        "missed": missed,
        "percent": (
            round(submitted * 100.0 / len(assignments), 1) if assignments else None
        ),
    }


def build_report(student_id, period: str = WEEKLY, start: date | None = None,
                 end: date | None = None, include_fees: bool = False) -> dict:
    """
    One child's digest for a period.

    `include_fees` is off by default and is decided per recipient by the caller, not here:
    the fee figures go only to a parent whose link grants `may_view_fees`, and baking that
    into the report would mean the same document could not be reused for two recipients with
    different permissions.
    """
    if start is None or end is None:
        start, end = period_bounds(period)

    student = firestore_users.get_document(str(student_id)) or {}
    enrollments = firestore_student_enrollments.query_documents(
        "student_id", "==", int(student_id)
    )
    class_id = enrollments[0].get("class_id") if enrollments else None

    attendance = _attendance_summary(student_id, start, end)
    exams = _exam_summary(student_id, start, end)
    homework = _homework_summary(student_id, start, end)

    report = {
        "student_id": int(student_id),
        "student_name": student.get("full_name"),
        "admission_number": student.get("admission_number"),
        "class_id": class_id,
        "class_name": (
            firestore_classes.get_document(str(class_id)) or {}
        ).get("name") if class_id else None,
        "period": period,
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "attendance": attendance,
        "exams": exams,
        "homework": homework,
        "fees": None,
        # Nothing happened in the window - no register marked, no exam sat, no homework due.
        # See the module docstring for why these are not sent.
        "is_empty": (
            attendance["total_sessions"] == 0
            and exams["count"] == 0
            and homework["set"] == 0
        ),
    }

    if include_fees:
        from app.services import billing
        invoices = billing.invoices_for_student(student_id)
        if invoices:
            latest = billing.present_invoice(invoices[0])
            report["fees"] = {
                "currency": latest["currency"],
                "total_amount": latest["total_amount"],
                "amount_paid": latest["amount_paid"],
                "amount_outstanding": latest["amount_outstanding"],
                "is_overdue": latest["is_overdue"],
                "next_due": next(
                    (i["due_date"] for i in latest["instalments"]
                     if i["status"] not in ("PAID", "WAIVED")),
                    None,
                ),
            }

    return report


# ---------------------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------------------

def _pct(value) -> str:
    return f"{value}%" if value is not None else "-"


def render_text(report: dict) -> str:
    """
    The plain-text body.

    Written first and rendered to HTML from the same figures, so a client that strips HTML
    gets the same numbers rather than an empty message.
    """
    a, e, h = report["attendance"], report["exams"], report["homework"]

    lines = [
        f"Progress report for {report['student_name']}"
        + (f" ({report['class_name']})" if report.get("class_name") else ""),
        f"{report['period'].title()}: {report['from_date']} to {report['to_date']}",
        "",
        "ATTENDANCE",
        f"  Present {a['present']} of {a['total_sessions']} sessions ({_pct(a['percent'])})",
        f"  Absent {a['absent']}, late {a['late']}, excused {a['excused']}",
        "",
        "EXAMS",
    ]

    if e["count"]:
        lines.append(f"  {e['count']} exam(s), average {_pct(e['average_percent'])}")
        if e["missed"]:
            lines.append(f"  {e['missed']} missed")
        for row in e["exams"]:
            mark = (
                f"{row['marks']}/{row['max_marks']}"
                if row["marks"] is not None and row["max_marks"] else (row["grade"] or row["status"])
            )
            lines.append(f"  - {row['subject_name'] or 'Exam'}: {row['exam_title']} - {mark}")
    else:
        lines.append("  No exams this period.")

    lines += [
        "",
        "HOMEWORK",
        f"  Submitted {h['submitted']} of {h['set']} ({_pct(h['percent'])})",
    ]
    if h["missed"]:
        lines.append(f"  {h['missed']} not handed in")
    if h["late"]:
        lines.append(f"  {h['late']} handed in late")

    if report.get("fees"):
        f = report["fees"]
        lines += [
            "",
            "FEES",
            f"  Outstanding: {f['currency']} {f['amount_outstanding']}",
        ]
        if f.get("next_due"):
            lines.append(f"  Next instalment due: {f['next_due']}")
        if f.get("is_overdue"):
            lines.append("  An instalment is overdue.")

    lines += ["", "Sent automatically by the school. Please reply to your class teacher "
                  "with any questions."]
    return "\n".join(lines)


def render_html(report: dict) -> str:
    a, e, h = report["attendance"], report["exams"], report["homework"]

    rows = "".join(
        f"<tr><td>{r['subject_name'] or ''}</td><td>{r['exam_title']}</td>"
        f"<td>{r['marks'] if r['marks'] is not None else (r['grade'] or r['status'])}"
        f"{'/' + str(r['max_marks']) if r['marks'] is not None and r['max_marks'] else ''}</td></tr>"
        for r in e["exams"]
    ) or "<tr><td colspan='3'>No exams this period.</td></tr>"

    fees_block = ""
    if report.get("fees"):
        f = report["fees"]
        fees_block = (
            f"<h3>Fees</h3><p>Outstanding: <strong>{f['currency']} "
            f"{f['amount_outstanding']}</strong>"
            + (f"<br>Next instalment due: {f['next_due']}" if f.get("next_due") else "")
            + ("<br><strong>An instalment is overdue.</strong>" if f.get("is_overdue") else "")
            + "</p>"
        )

    return f"""<div style="font-family:system-ui,sans-serif;max-width:640px">
<h2>Progress report - {report['student_name']}</h2>
<p style="color:#555">{report.get('class_name') or ''} &middot;
{report['period'].title()}: {report['from_date']} to {report['to_date']}</p>

<h3>Attendance</h3>
<p>Present <strong>{a['present']} of {a['total_sessions']}</strong> ({_pct(a['percent'])})<br>
Absent {a['absent']} &middot; Late {a['late']} &middot; Excused {a['excused']}</p>

<h3>Exams</h3>
<p>{e['count']} exam(s), average <strong>{_pct(e['average_percent'])}</strong>
{f", {e['missed']} missed" if e['missed'] else ""}</p>
<table cellpadding="6" style="border-collapse:collapse;width:100%">
<tr style="text-align:left;background:#f4f4f6"><th>Subject</th><th>Exam</th><th>Result</th></tr>
{rows}
</table>

<h3>Homework</h3>
<p>Submitted <strong>{h['submitted']} of {h['set']}</strong> ({_pct(h['percent'])})
{f"<br>{h['missed']} not handed in" if h['missed'] else ""}
{f"<br>{h['late']} handed in late" if h['late'] else ""}</p>

{fees_block}
<p style="color:#777;font-size:13px">Sent automatically by the school. Please reply to your
class teacher with any questions.</p>
</div>"""


# ---------------------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------------------

def _claim(key: str, payload: dict) -> bool:
    """
    Claims the right to send, atomically.

    `create_document` maps onto Firestore's `create` and fails when the id is taken - the
    same mechanism the class reminders use, and the reason two workers waking up on Sunday
    night send one email rather than two.

    It also returns False when Firestore is unreachable, which reads here as "do not send".
    That is the right way round: a digest that is late because the database was down is a
    nuisance, and one sent twice because the claim could not be recorded is a complaint.
    """
    return firestore_reminder_log.create_document(key, payload)


def send_report(student_id, period: str = WEEKLY, start: date | None = None,
                end: date | None = None, force: bool = False) -> dict:
    """
    Builds and emails one child's digest to every parent entitled to it.

    Each recipient gets a report built for their own permissions - the fee section appears
    only for a link with `may_view_fees`. Returns what happened, per recipient, rather than a
    bare count: "sent to 1 of 3" is not actionable, and the two that failed are the whole
    point of the response.
    """
    if start is None or end is None:
        start, end = period_bounds(period)

    recipients = family_service.report_recipients(student_id)
    if not recipients:
        return {
            "student_id": int(student_id), "sent": 0, "skipped": "no recipients",
            "results": [],
        }

    base = build_report(student_id, period, start, end)
    if base["is_empty"] and not force:
        return {
            "student_id": int(student_id), "sent": 0,
            "skipped": "nothing happened in this period",
            "results": [],
        }

    if not mail_is_configured():
        return {
            "student_id": int(student_id), "sent": 0,
            "skipped": "mail is not configured", "results": [],
            "report": base,
        }

    results = []
    sent = 0
    for recipient in recipients:
        key = report_key(student_id, period, start, f"EMAIL:{recipient['email']}")
        if not force and not _claim(key, {
            "type": "parent_report",
            "student_id": int(student_id),
            "period": period,
            "from_date": start.isoformat(),
            "to_date": end.isoformat(),
            "email": recipient["email"],
            "claimed_at": datetime.utcnow().isoformat(),
        }):
            results.append({"email": recipient["email"], "sent": False,
                            "detail": "already sent for this period"})
            continue

        # Rebuilt per recipient: the fee section is governed by that parent's own link.
        may_see_fees = False
        if recipient.get("user_id") is not None:
            link = family_service.link_between(recipient["user_id"], student_id)
            may_see_fees = bool(link and link.get("may_view_fees"))

        report = build_report(student_id, period, start, end, include_fees=may_see_fees)
        subject = (
            f"{report['student_name']} - {period.title()} progress report "
            f"({report['from_date']} to {report['to_date']})"
        )

        ok = send_email(
            to=recipient["email"],
            subject=subject,
            body_text=render_text(report),
            body_html=render_html(report),
        )
        sent += 1 if ok else 0
        results.append({
            "email": recipient["email"],
            "sent": bool(ok),
            "detail": "sent" if ok else "the mail server rejected it",
        })

    return {
        "student_id": int(student_id),
        "student_name": base["student_name"],
        "period": period,
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "sent": sent,
        "results": results,
    }


def sweep(period: str = WEEKLY, class_id=None, force: bool = False,
          on: date | None = None) -> dict:
    """
    Sends the digest for every active student.

    Safe to run more than once: the log claim makes a repeat a no-op per recipient, so a
    scheduler that fires twice, or an admin who presses the button after it already ran,
    costs nothing.
    """
    start, end = period_bounds(period, on)

    students = [
        u for u in firestore_users.list_all()
        if u.get("role") == "STUDENT" and u.get("is_active", True)
    ]
    if class_id is not None:
        wanted = {
            int(e["student_id"]) for e in firestore_student_enrollments.list_all()
            if int(e.get("class_id", -1)) == int(class_id) and e.get("student_id") is not None
        }
        students = [u for u in students if int(u["id"]) in wanted]

    sent = skipped = 0
    details = []
    for student in students:
        try:
            outcome = send_report(student["id"], period, start, end, force)
        except Exception as exc:  # pragma: no cover - one bad record must not stop the sweep
            logger.warning("Parent report failed for student %s: %s", student["id"], exc)
            skipped += 1
            continue

        sent += outcome["sent"]
        if outcome["sent"] == 0:
            skipped += 1
        details.append(outcome)

    logger.info("Parent report sweep (%s): %s email(s) sent, %s student(s) skipped.",
                period, sent, skipped)
    return {
        "period": period,
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "students_considered": len(students),
        "emails_sent": sent,
        "students_skipped": skipped,
        "details": details,
    }
