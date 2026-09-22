"""
Tuition fees: what a student buys, and what they owe for the classes they took.

Admin-only, in every direction. Teachers and students never read the pricing here - it is
not exposed on their routers at all - because a teacher seeing what a student is charged, or
a student seeing what a teacher is paid, is a conversation nobody asked this system to start.
(A student may read their *own* usage and bill; see `tuition_students`.)

**The fee is a package, not a rate per subject.** A student buys "30 classes for 15,000" and
spends those classes on whichever subjects they take - maths on Monday and physics on
Thursday come out of the same allowance. So there is one line on an invoice, for the
package, and the per-subject class counts sit *inside* it, which is what keeps a bill
explicable three months later: "14 classes this period - 6 English, 5 Maths, 3 Science -
at 500 a class."

The package is assigned per **term**. The year runs April to March in two terms (see
`app.services.admissions`), and a student's usage - classes used, classes remaining - is
counted from the start of the term the invoice period falls in. A student on a different
package next term gets a new assignment; the old one keeps its dates so last term's invoice
still reads back against the package that was actually in force.

Two ways to turn a package into money, chosen per package (`PackageBillingMode`):

  * PER_CLASS - each period bills the classes attended in it at `amount / classes_included`.
                "Bill based on the total classes attended", in the brief's words.
  * PACKAGE   - the flat amount is billed once per term, on the first invoice raised in it;
                later invoices in that term bill only classes beyond the allowance.

A DRAFT invoice is recomputed from the sessions every time it is regenerated. Once ISSUED the
numbers are frozen, because a bill that silently changes after it was sent is not a bill.
"""

import logging
from datetime import date, datetime, timedelta

from fastapi import HTTPException

from app.core.enums import (
    TERM_NAMES, InvoiceStatus, PackageBillingMode, Program,
)
from app.core.firebase import (
    firestore_subjects, firestore_tuition_invoices, firestore_tuition_package_assignments,
    firestore_tuition_packages, firestore_tuition_sessions, firestore_users,
)
from app.services import admissions as admission_service
from app.services.tuition.common import now_utc, parse_date, store_dt, user_id_of
from app.services.tuition.enrollments import for_student
from app.services.tuition.reports import summarize
from app.services.tuition.sessions import filter_sessions
from app.services.tuition.settings_store import (
    finance_settings, resolve_currency, tuition_settings,
)

logger = logging.getLogger("tuition.fees")


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _enum_value(value):
    return value.value if hasattr(value, "value") else value


def _term_name(term_key: str | None) -> str | None:
    return TERM_NAMES.get(str(term_key)) if term_key else None


# ---------------------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------------------

def require_package(package_id) -> dict:
    package = firestore_tuition_packages.get_document(str(package_id))
    if not package:
        raise HTTPException(status_code=404, detail=f"Package {package_id} not found")
    return package


def per_class_amount(package: dict) -> float:
    """The package's per-class rate: what one class of the allowance costs."""
    included = int(package.get("classes_included") or 0)
    if included <= 0:
        return 0.0
    return round(float(package.get("amount") or 0) / included, 2)


def _assert_tuition_year(academic_year_id) -> int | None:
    """
    A year the package is scoped to must be one the tuition programme runs in. A package
    scoped to a school-only year would never resolve, and the admin would be left wondering
    why their price was ignored.
    """
    if academic_year_id is None:
        return None
    year = admission_service.require_year(academic_year_id)
    if Program.TUITION.value not in admission_service._programs(year.get("programs")):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Academic year '{year.get('name')}' does not apply to the tuition "
                "programme. Add TUITION to its programs, or use a tuition year."
            ),
        )
    return int(academic_year_id)


def create_package(payload, actor) -> dict:
    academic_year_id = _assert_tuition_year(getattr(payload, "academic_year_id", None))

    package_id = firestore_tuition_packages.get_next_numeric_id()
    document = {
        "name": payload.name.strip(),
        "amount": float(payload.amount),
        "classes_included": int(payload.classes_included),
        "currency": (payload.currency or tuition_settings()["currency"]).upper(),
        "billing_mode": _enum_value(payload.billing_mode),
        "academic_year_id": academic_year_id,
        "term": _enum_value(payload.term),
        "max_subjects": payload.max_subjects,
        "count_missed_classes": bool(payload.count_missed_classes),
        "is_active": bool(payload.is_active),
        "notes": payload.notes,
        "created_by": user_id_of(actor),
        "created_at": _now_iso(),
    }
    firestore_tuition_packages.add_document(str(package_id), document)
    document["id"] = package_id
    logger.info("Created tuition package %s (%s: %s for %s classes).",
                package_id, document["name"], document["amount"], document["classes_included"])
    return document


def update_package(package: dict, payload) -> dict:
    changed = payload.model_dump(exclude_unset=True)
    updates = {}
    for field in ("name", "amount", "classes_included", "currency", "max_subjects",
                  "count_missed_classes", "is_active", "notes"):
        if field in changed:
            updates[field] = changed[field]
    if "academic_year_id" in changed:
        updates["academic_year_id"] = _assert_tuition_year(changed["academic_year_id"])
    for field in ("billing_mode", "term"):
        if field in changed:
            updates[field] = _enum_value(changed[field])
    if "amount" in updates and updates["amount"] is not None:
        updates["amount"] = float(updates["amount"])
    if "classes_included" in updates and updates["classes_included"] is not None:
        updates["classes_included"] = int(updates["classes_included"])
    if "currency" in updates and updates["currency"]:
        updates["currency"] = str(updates["currency"]).upper()
    if not updates:
        return package
    updates["updated_at"] = _now_iso()
    firestore_tuition_packages.add_document(str(package["id"]), updates)
    return {**package, **updates}


def delete_package(package: dict) -> None:
    """
    Removes a package nobody is on.

    Refused while a live assignment points at it: the students on it would silently drop to
    "no package" and their next invoice would price every class at zero. End or reassign
    them first. Invoices already generated keep the figures they were built with, because
    the price is copied onto the invoice line rather than looked up when the bill is read.
    """
    live = [
        a for a in firestore_tuition_package_assignments.query_documents(
            "package_id", "==", int(package["id"])
        )
        if a.get("is_active", True)
    ]
    if live:
        raise HTTPException(
            status_code=409,
            detail=f"{len(live)} student(s) are on '{package.get('name')}'. Reassign them "
                   "before deleting it, or mark it inactive instead.",
        )
    firestore_tuition_packages.delete_document(str(package["id"]))


def list_packages(academic_year_id=None, term: str | None = None,
                  include_inactive: bool = True) -> list[dict]:
    """
    Packages on offer.

    A package with no year appears in every year's list, and one with no term in both terms'
    lists, because that is what an unscoped package is - the offer that stands unless a
    narrower one replaces it.
    """
    packages = firestore_tuition_packages.list_all()
    if academic_year_id is not None:
        packages = [
            p for p in packages
            if p.get("academic_year_id") in (None, "", int(academic_year_id))
        ]
    if term:
        packages = [p for p in packages if p.get("term") in (None, "", str(term))]
    if not include_inactive:
        packages = [p for p in packages if p.get("is_active", True)]
    packages.sort(key=lambda p: (str(p.get("name") or ""), int(p.get("id") or 0)))
    return packages


def present_package(package: dict) -> dict:
    """A package as the API returns it, with its year's name and the derived rate resolved."""
    year_name = None
    if package.get("academic_year_id") is not None:
        year = admission_service.firestore_academic_years.get_document(
            str(package["academic_year_id"])
        ) or {}
        year_name = year.get("name")

    assigned = [
        a for a in firestore_tuition_package_assignments.query_documents(
            "package_id", "==", int(package["id"])
        )
        if a.get("is_active", True)
    ]
    return {
        **package,
        "id": int(package["id"]),
        "billing_mode": package.get("billing_mode") or PackageBillingMode.PER_CLASS.value,
        "classes_included": int(package.get("classes_included") or 0),
        "amount": float(package.get("amount") or 0),
        "per_class_amount": per_class_amount(package),
        "academic_year_name": year_name,
        "term_name": _term_name(package.get("term")),
        "count_missed_classes": bool(package.get("count_missed_classes", True)),
        "students_assigned": len(assigned),
    }


# ---------------------------------------------------------------------------------------
# Assignments: who is on which package, for which term
# ---------------------------------------------------------------------------------------

def require_assignment(assignment_id) -> dict:
    assignment = firestore_tuition_package_assignments.get_document(str(assignment_id))
    if not assignment:
        raise HTTPException(status_code=404, detail=f"Package assignment {assignment_id} not found")
    return assignment


def assignments_for_student(student_id, include_inactive: bool = False) -> list[dict]:
    records = firestore_tuition_package_assignments.query_documents(
        "student_id", "==", int(student_id)
    )
    if not include_inactive:
        records = [a for a in records if a.get("is_active", True)]
    return sorted(records, key=lambda a: (str(a.get("starts_on") or ""), int(a.get("id") or 0)),
                  reverse=True)


def _covers(assignment: dict, on: date) -> bool:
    start = parse_date(assignment.get("starts_on"))
    end = parse_date(assignment.get("ends_on"))
    if start and on < start:
        return False
    if end and on > end:
        return False
    return True


def resolve_assignment(student_id, on: date) -> dict | None:
    """
    The assignment in force on a date, most specific first.

    A term-scoped assignment beats a year-scoped one, which beats an open-ended one, and
    ties go to the newest. Returns None when nothing covers the date, which the pricing
    treats as "no package": the classes are still counted, and priced at zero, so the
    admin sees what they have not yet put a price on rather than a bill that hides it.
    """
    candidates = [a for a in assignments_for_student(student_id) if _covers(a, on)]
    if not candidates:
        return None
    candidates.sort(
        key=lambda a: (
            2 if a.get("term") else 0,
            1 if a.get("academic_year_id") is not None else 0,
            int(a.get("id") or 0),
        ),
        reverse=True,
    )
    return candidates[0]


def assign_package(student_id, payload, actor) -> dict:
    """
    Puts a student on a package for a term.

    The year and term default from the package's own scope, then from the calendar: a
    package with no term assigned today lands in whichever term today falls in. The
    assignment's dates are the term's, so usage is counted within the term and a new term
    starts a fresh allowance. Assigning again for the same (year, term) replaces the
    previous assignment rather than stacking a second package on the same classes.
    """
    from app.services.tuition.enrollments import require_tuition_user

    student = require_tuition_user(int(student_id), "STUDENT", "Student")
    package = require_package(payload.package_id)
    if not package.get("is_active", True):
        raise HTTPException(
            status_code=400, detail=f"Package '{package.get('name')}' is inactive."
        )

    anchor = parse_date(getattr(payload, "starts_on", None)) or date.today()

    # --- Year --------------------------------------------------------------------------
    academic_year_id = getattr(payload, "academic_year_id", None)
    if academic_year_id is None:
        academic_year_id = package.get("academic_year_id")
    if academic_year_id is None:
        year = (
            admission_service.year_for_date(anchor, Program.TUITION.value)
            or admission_service.current_year(Program.TUITION.value)
        )
        academic_year_id = year["id"] if year else None
    if package.get("academic_year_id") is not None and academic_year_id is not None \
            and int(package["academic_year_id"]) != int(academic_year_id):
        raise HTTPException(
            status_code=400,
            detail=f"Package '{package.get('name')}' is scoped to another academic year.",
        )
    year = (
        admission_service.require_year(academic_year_id)
        if academic_year_id is not None else None
    )
    academic_year_id = int(year["id"]) if year else None

    # --- Term --------------------------------------------------------------------------
    term = _enum_value(getattr(payload, "term", None)) or package.get("term")
    if term is None and year:
        found = admission_service.term_for_date(year, anchor)
        term = found["key"] if found else None
    if package.get("term") and term and str(package["term"]) != str(term):
        raise HTTPException(
            status_code=400,
            detail=f"Package '{package.get('name')}' is a {_term_name(package['term'])} "
                   f"package; it cannot be assigned for {_term_name(term)}.",
        )

    # --- Window ------------------------------------------------------------------------
    starts_on = parse_date(getattr(payload, "starts_on", None))
    ends_on = parse_date(getattr(payload, "ends_on", None))
    if year:
        window_start, window_end = admission_service.term_window(year, term)
        starts_on = starts_on or window_start
        ends_on = ends_on or window_end
    if starts_on and ends_on and ends_on < starts_on:
        raise HTTPException(status_code=400, detail="ends_on cannot be earlier than starts_on.")

    # --- Replace whatever was there for this term ----------------------------------------
    replaced = []
    for existing in assignments_for_student(student_id):
        same_year = (existing.get("academic_year_id") in (None, "")) == (academic_year_id is None) \
            and (academic_year_id is None or int(existing.get("academic_year_id") or -1) == academic_year_id)
        same_term = (existing.get("term") or None) == (term or None)
        if same_year and same_term:
            firestore_tuition_package_assignments.add_document(str(existing["id"]), {
                "is_active": False,
                "ended_at": _now_iso(),
                "ended_by": user_id_of(actor),
                "replaced_reason": "Reassigned",
            })
            replaced.append(int(existing["id"]))

    assignment_id = firestore_tuition_package_assignments.get_next_numeric_id()
    document = {
        "student_id": int(student_id),
        "package_id": int(package["id"]),
        "academic_year_id": academic_year_id,
        "term": term,
        "starts_on": starts_on.isoformat() if starts_on else None,
        "ends_on": ends_on.isoformat() if ends_on else None,
        "is_active": True,
        "notes": getattr(payload, "notes", None),
        "assigned_by": user_id_of(actor),
        "created_at": _now_iso(),
    }
    firestore_tuition_package_assignments.add_document(str(assignment_id), document)
    document["id"] = assignment_id
    logger.info("Assigned package %s to student %s for %s/%s (replaced %s).",
                package["id"], student.get("id"), academic_year_id, term, replaced or "none")
    return document


def end_assignment(assignment: dict, actor) -> dict:
    updates = {
        "is_active": False,
        "ended_at": _now_iso(),
        "ended_by": user_id_of(actor),
    }
    firestore_tuition_package_assignments.add_document(str(assignment["id"]), updates)
    return {**assignment, **updates}


# ---------------------------------------------------------------------------------------
# Counting classes against the package
# ---------------------------------------------------------------------------------------

def _student_sessions(student_id) -> list[dict]:
    """Every class this student has, in one query. Filtered in memory per window."""
    return firestore_tuition_sessions.query_documents("student_id", "==", int(student_id))


def _classes_counted(counts: dict, count_missed: bool) -> int:
    """
    How many classes a set of counts spends from the package.

    Billable classes are the ones the teacher actually held (plus any an admin overrode);
    a teacher's no-show never counts. Whether the *student's* no-show counts is the
    package's call - by default it does, because the slot was spent either way.
    """
    billable = int(counts.get("billable_sessions") or 0)
    if not count_missed:
        billable = max(billable - int(counts.get("missed") or 0), 0)
    return billable


def _usage_window(assignment: dict | None, year: dict | None, term_key: str | None,
                  period_start: date) -> date | None:
    """Where usage counting starts: the assignment's own date, else the term's, else the year's."""
    if assignment and parse_date(assignment.get("starts_on")):
        return parse_date(assignment["starts_on"])
    if year:
        start, _ = admission_service.term_window(year, term_key)
        if start:
            return start
    return None


def package_usage(student_id, assignment: dict | None, package: dict | None,
                  upto: date, period_start: date | None = None,
                  year: dict | None = None, sessions: list[dict] | None = None) -> dict:
    """
    Classes used and remaining on a student's package, as of a date.

    Counted from the assignment's start (the term start, normally) up to `upto`. When a
    `period_start` is given the classes before it are reported separately, which is what an
    invoice needs to say "12 used before this period, 5 in it, 13 remaining".
    """
    sessions = _student_sessions(student_id) if sessions is None else sessions
    count_missed = bool((package or {}).get("count_missed_classes", True))
    term_key = (assignment or {}).get("term")
    window_start = _usage_window(assignment, year, term_key, period_start or upto)

    used_to_date = _classes_counted(summarize(filter_sessions(sessions, window_start, upto)),
                                    count_missed)
    used_before = 0
    if period_start:
        day_before = period_start - timedelta(days=1)
        if window_start is None or day_before >= window_start:
            used_before = _classes_counted(
                summarize(filter_sessions(sessions, window_start, day_before)), count_missed
            )

    included = int((package or {}).get("classes_included") or 0) if package else None
    return {
        "usage_from": window_start.isoformat() if window_start else None,
        "usage_to": upto.isoformat(),
        "classes_included": included,
        "classes_used_before_period": used_before,
        "classes_used_to_date": used_to_date,
        "classes_remaining": max(included - used_to_date, 0) if included is not None else None,
        "classes_over": max(used_to_date - included, 0) if included is not None else 0,
    }


def _package_already_billed(student_id, assignment_id, exclude_invoice_id: str) -> str | None:
    """
    The invoice that already carries this assignment's flat package charge, if any.

    Under PACKAGE billing the amount is charged once per term. Scanning the student's live
    invoices for the charge, rather than storing a "billed" flag on the assignment, means
    cancelling that invoice frees the charge to be raised again - which is what cancelling
    is for.
    """
    if assignment_id is None:
        return None
    for invoice in firestore_tuition_invoices.query_documents("student_id", "==", int(student_id)):
        if str(invoice.get("id")) == str(exclude_invoice_id):
            continue
        if invoice.get("status") == InvoiceStatus.CANCELLED.value:
            continue
        if int(invoice.get("assignment_id") or -1) != int(assignment_id):
            continue
        for line in invoice.get("line_items") or []:
            if float(line.get("package_amount") or 0) > 0:
                return str(invoice.get("id"))
    return None


def _subject_rows(student_id, sessions: list[dict], period_start: date,
                  period_end: date, count_missed: bool) -> list[dict]:
    """The per-subject counts inside the package line - what makes the bill checkable."""
    rows = []
    for enrollment in for_student(student_id, include_inactive=True):
        mine = [s for s in sessions if str(s.get("enrollment_id")) == str(enrollment.get("id"))]
        counts = summarize(filter_sessions(mine, period_start, period_end))
        if not counts["total_sessions"]:
            continue
        subject = firestore_subjects.get_document(str(enrollment.get("subject_id"))) or {}
        teacher = firestore_users.get_document(str(enrollment.get("teacher_id"))) or {}
        rows.append({
            "enrollment_id": enrollment["id"],
            "subject_id": enrollment.get("subject_id"),
            "subject_name": subject.get("name"),
            "subject_code": subject.get("code"),
            "teacher_id": enrollment.get("teacher_id"),
            "teacher_name": teacher.get("full_name"),
            "classes_counted": _classes_counted(counts, count_missed),
            "sessions_conducted": counts["conducted"],
            "sessions_billable": counts["billable_sessions"],
            "sessions_attended": counts["attended"],
            "sessions_missed": counts["missed"],
            "sessions_cancelled": counts["cancelled"],
            "teacher_no_show": counts["teacher_no_show"],
            "taught_minutes": counts["taught_minutes"],
        })
    rows.sort(key=lambda r: str(r.get("subject_name") or ""))
    return rows


# ---------------------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------------------

def year_for_period(period_start: date) -> dict | None:
    """
    The tuition academic year an invoice period falls in.

    Resolved from the period's own start date rather than from `current_year()`, because an
    invoice raised in April for March's classes belongs to March's year and the current-year
    flag has usually moved on by then. Falls back to the flagged current year only when no
    year's dates cover the period - a programme that has created years but left their ranges
    wrong should still bill something.
    """
    return (
        admission_service.year_for_date(period_start, Program.TUITION.value)
        or admission_service.current_year(Program.TUITION.value)
    )


def price_package(student_id, period_start: date, period_end: date,
                  year: dict | None, exclude_invoice_id: str | None = None,
                  sessions: list[dict] | None = None) -> dict:
    """
    Prices one student's period against their package, returning the line with its workings.

    Always returns a line, even with no package or no classes: the counts are the point, and
    an admin looking at a student they have not priced yet needs to see the classes that are
    waiting to be charged. The caller decides whether a zero line is worth showing.
    """
    sessions = _student_sessions(student_id) if sessions is None else sessions

    assignment = resolve_assignment(student_id, period_start)
    package = None
    if assignment:
        package = firestore_tuition_packages.get_document(str(assignment.get("package_id")))

    term_key = (assignment or {}).get("term")
    if term_key is None and year:
        found = admission_service.term_for_date(year, period_start)
        term_key = found["key"] if found else None

    count_missed = bool((package or {}).get("count_missed_classes", True))
    period_counts = summarize(filter_sessions(sessions, period_start, period_end))
    classes_billed = _classes_counted(period_counts, count_missed)
    usage = package_usage(student_id, assignment, package, period_end, period_start, year, sessions)

    rate = per_class_amount(package) if package else 0.0
    mode = (package or {}).get("billing_mode") or PackageBillingMode.PER_CLASS.value
    included = int(package.get("classes_included") or 0) if package else 0

    package_amount = 0.0
    overage_classes = 0
    already_billed_on = None
    note = None

    if not package:
        amount = 0.0
        note = "No package is assigned for this period; classes are counted but not priced."
    elif mode == PackageBillingMode.PACKAGE.value:
        exclude = exclude_invoice_id or invoice_id(student_id, period_start, period_end)
        already_billed_on = _package_already_billed(student_id, assignment.get("id"), exclude)
        if already_billed_on:
            note = f"Package already billed on invoice {already_billed_on}."
        else:
            package_amount = float(package.get("amount") or 0)
        # Classes beyond the allowance, spent in this period, at the per-class rate.
        over_before = max(usage["classes_used_before_period"] - included, 0)
        overage_classes = max(usage["classes_over"] - over_before, 0)
        amount = package_amount + overage_classes * rate
    else:
        amount = classes_billed * rate

    return {
        "package_id": int(package["id"]) if package else None,
        "package_name": package.get("name") if package else None,
        "assignment_id": int(assignment["id"]) if assignment else None,
        "billing_mode": mode if package else None,
        "term": term_key,
        "term_name": _term_name(term_key),
        "classes_included": included if package else None,
        "unit_amount": rate,
        "unit_label": "class",
        "quantity": classes_billed,
        "classes_billed": classes_billed,
        "classes_used_before_period": usage["classes_used_before_period"],
        "classes_used_to_date": usage["classes_used_to_date"],
        "classes_remaining": usage["classes_remaining"],
        "usage_from": usage["usage_from"],
        "package_amount": round(package_amount, 2),
        "overage_classes": overage_classes,
        "overage_amount": round(overage_classes * rate, 2),
        "already_billed_on": already_billed_on,
        "note": note,
        # The counts the price came from, carried on the line so the bill explains itself.
        "sessions_conducted": period_counts["conducted"],
        "sessions_billable": period_counts["billable_sessions"],
        "sessions_attended": period_counts["attended"],
        "sessions_missed": period_counts["missed"],
        "sessions_cancelled": period_counts["cancelled"],
        "teacher_no_show": period_counts["teacher_no_show"],
        "taught_minutes": period_counts["taught_minutes"],
        "subjects": _subject_rows(student_id, sessions, period_start, period_end, count_missed),
        "amount": round(amount, 2),
    }


# ---------------------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------------------

def invoice_id(student_id, period_start: date, period_end: date) -> str:
    """
    Derived from student and period, so regenerating a month corrects the bill rather than
    issuing a second one for the same classes - the way duplicate invoices actually happen.
    """
    return f"{student_id}_{period_start.isoformat()}_{period_end.isoformat()}"


def require_invoice(invoice_id_value) -> dict:
    invoice = firestore_tuition_invoices.get_document(str(invoice_id_value))
    if not invoice:
        raise HTTPException(status_code=404, detail=f"Invoice {invoice_id_value} not found")
    return invoice


def generate_invoice(student_id: int, period_start, period_end, actor,
                     discount_amount: float = 0.0, tax_amount: float | None = None,
                     due_date=None, notes: str | None = None,
                     currency: str | None = None) -> dict:
    """
    Builds (or rebuilds) a student's bill for a period from their classes and package.

    Built from `compute_breakdown`, so the invoice and the fee page a parent was looking at
    five minutes earlier are the same arithmetic - tax, convenience charge and rounding
    included, at the chosen currency's own rates.

    `tax_amount` overrides the computed tax when given, for the period an administrator has
    to reconcile by hand; leave it out and the currency's tax settings decide. That is the
    only manual override, and it is recorded on the invoice as one.

    Refuses to overwrite an invoice that has already been issued. That is the one hard rule
    in this module: regenerating a draft is routine housekeeping, silently restating a bill
    somebody has already been sent is not, and the two are one careless button apart.
    """
    start = parse_date(period_start)
    end = parse_date(period_end)
    if not start or not end:
        raise HTTPException(status_code=400, detail="period_start and period_end are required.")
    if end < start:
        raise HTTPException(status_code=400, detail="period_end cannot be earlier than period_start.")

    student = firestore_users.get_document(str(student_id))
    if not student:
        raise HTTPException(status_code=404, detail=f"Student {student_id} not found")

    document_id = invoice_id(student_id, start, end)
    existing = firestore_tuition_invoices.get_document(document_id)
    if existing and existing.get("status") != InvoiceStatus.DRAFT.value:
        raise HTTPException(
            status_code=409,
            detail=f"Invoice {document_id} is already {existing.get('status')} and cannot be "
                   f"regenerated. Cancel it first if the figures are wrong.",
        )

    breakdown = compute_breakdown(
        student_id, start, end, currency=currency, discount_amount=discount_amount
    )

    # A manual tax figure replaces the computed one and the total is rebuilt around it, so an
    # override cannot leave the total disagreeing with the line it came from.
    tax_override = tax_amount is not None
    if tax_override:
        payable = round(
            breakdown["subtotal"] - breakdown["discount_total"] + float(tax_amount or 0), 2
        )
        total = _apply_rounding(
            round(payable + breakdown["convenience_total"], 2),
            resolve_currency(Program.TUITION.value, currency)["rounding"],
        )
    else:
        total = breakdown["total_amount"]

    document = {
        "student_id": student_id,
        "student_name": student.get("full_name"),
        "admission_number": student.get("admission_number"),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "status": InvoiceStatus.DRAFT.value,

        # Currency and rate, frozen when the invoice is issued. A bill that re-converts at
        # today's rate is a bill whose total changes after it was sent.
        "currency": breakdown["currency"],
        "currency_symbol": breakdown["currency_symbol"],
        "base_currency": breakdown["base_currency"],
        "exchange_rate": breakdown["exchange_rate"],

        # Which year, term and package produced these figures. Stored rather than re-derived,
        # because all three can be edited afterwards and an issued bill must stay explicable
        # against what it was actually priced from.
        "academic_year_id": breakdown["academic_year_id"],
        "academic_year_name": breakdown["academic_year_name"],
        "term": breakdown["term"],
        "term_name": breakdown["term_name"],
        "package_id": breakdown["package_id"],
        "package_name": breakdown["package_name"],
        "assignment_id": breakdown["assignment_id"],
        "billing_mode": breakdown["billing_mode"],
        "classes_included": breakdown["classes_included"],
        "classes_billed": breakdown["classes_billed"],
        "classes_used_to_date": breakdown["classes_used_to_date"],
        "classes_remaining": breakdown["classes_remaining"],

        "line_items": breakdown["line_items"],
        "subtotal": breakdown["subtotal"],
        "discount_amount": breakdown["discount_total"],
        "tax_amount": float(tax_amount) if tax_override else breakdown["tax_total"],
        "tax_label": breakdown["tax_label"],
        "tax_is_manual": tax_override,
        "taxable_base": breakdown["taxable_base"],
        "convenience_amount": breakdown["convenience_total"],
        "total_amount": total,
        "amount_paid": float((existing or {}).get("amount_paid") or 0),
        "payments": (existing or {}).get("payments") or [],
        "due_date": parse_date(due_date).isoformat() if parse_date(due_date) else None,
        "notes": notes,
        "generated_by": user_id_of(actor),
        "created_at": (existing or {}).get("created_at") or _now_iso(),
        "updated_at": _now_iso(),
    }
    firestore_tuition_invoices.add_document(document_id, document)
    document["id"] = document_id
    return document


def generate_batch(period_start, period_end, actor) -> dict:
    """
    Bills every student with classes in a period.

    An already-issued invoice is skipped and reported rather than failing the run: the point
    of a monthly batch is that it can be re-run after fixing one student's data without the
    admin having to work out which of sixty students it got to last time.
    """
    start = parse_date(period_start)
    end = parse_date(period_end)
    from app.services.tuition.reports import programme_report

    report = programme_report(start, end)
    generated, skipped = [], []

    for row in report["students"]:
        try:
            invoice = generate_invoice(row["student_id"], start, end, actor)
            generated.append({"student_id": row["student_id"], "invoice_id": invoice["id"],
                              "total_amount": invoice["total_amount"]})
        except HTTPException as exc:
            skipped.append({"student_id": row["student_id"], "reason": str(exc.detail)})

    return {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "generated": generated,
        "skipped": skipped,
        "total_billed": round(sum(row["total_amount"] for row in generated), 2),
    }


def _settlement_status(paid: float, total: float, fallback: str) -> str:
    """
    Where an issued invoice stands against what has been paid.

    Derived on every write rather than stored independently, so the status can never drift
    out of step with the payments beneath it - the classic way an invoice ends up marked
    unpaid with three receipts attached.
    """
    if paid <= 0:
        return fallback
    if total > 0 and paid >= total:
        return InvoiceStatus.PAID.value
    return InvoiceStatus.PARTIALLY_PAID.value


def issue_invoice(invoice: dict, actor) -> dict:
    """
    Freezes a draft and marks it sent.

    Issuing an invoice already settled in advance is normal and lands straight on PAID: a
    student who paid up front does not need the bill to pretend otherwise for a moment
    first. What cannot happen is issuing something already issued - the figures are frozen
    at that point, and re-issuing would be a second bill for the same classes.
    """
    if invoice.get("status") != InvoiceStatus.DRAFT.value:
        raise HTTPException(status_code=409, detail=f"Invoice is already {invoice.get('status')}.")

    paid = float(invoice.get("amount_paid") or 0)
    total = float(invoice.get("total_amount") or 0)

    updates = {
        "status": _settlement_status(paid, total, InvoiceStatus.ISSUED.value),
        "issued_at": store_dt(now_utc()),
        "issued_by": user_id_of(actor),
        "updated_at": _now_iso(),
    }
    firestore_tuition_invoices.add_document(str(invoice["id"]), updates)
    return {**invoice, **updates}


def record_payment(invoice: dict, actor, amount: float, method: str | None = None,
                   reference: str | None = None, paid_at=None) -> dict:
    """
    Records money received and moves the invoice's status to match.

    Payments are appended rather than summed into a single field, so an invoice settled in
    three instalments shows three instalments. The status is derived from the running total
    on every write, which means it cannot drift out of step with the payments beneath it.
    """
    if invoice.get("status") == InvoiceStatus.CANCELLED.value:
        raise HTTPException(status_code=409, detail="A cancelled invoice cannot take payments.")
    if amount is None or float(amount) <= 0:
        raise HTTPException(status_code=400, detail="Payment amount must be greater than zero.")

    payments = list(invoice.get("payments") or [])
    payments.append({
        "amount": round(float(amount), 2),
        "paid_at": store_dt(paid_at or now_utc()),
        "method": method,
        "reference": reference,
        "recorded_by": user_id_of(actor),
    })

    paid = round(sum(float(p.get("amount") or 0) for p in payments), 2)
    total = float(invoice.get("total_amount") or 0)

    # A draft stays a draft, however much has been paid against it. Money received before
    # the bill was sent is an advance, not a settlement, and moving the invoice out of DRAFT
    # here would quietly do two wrong things: block the admin from regenerating the figures
    # after correcting a class, and block them from ever issuing it - a draft is the only
    # thing `issue_invoice` accepts. The payment is recorded either way and is applied the
    # moment the invoice is issued.
    if invoice.get("status") == InvoiceStatus.DRAFT.value:
        status = InvoiceStatus.DRAFT.value
    else:
        status = _settlement_status(paid, total, str(invoice.get("status")))

    updates = {
        "payments": payments,
        "amount_paid": paid,
        "status": status,
        "updated_at": _now_iso(),
    }
    firestore_tuition_invoices.add_document(str(invoice["id"]), updates)
    return {**invoice, **updates}


def cancel_invoice(invoice: dict, reason: str | None = None) -> dict:
    updates = {
        "status": InvoiceStatus.CANCELLED.value,
        "cancellation_reason": reason,
        "updated_at": _now_iso(),
    }
    firestore_tuition_invoices.add_document(str(invoice["id"]), updates)
    return {**invoice, **updates}


def present_invoice(invoice: dict) -> dict:
    """
    An invoice as the payer's screens return it: the stored record plus what is still owed
    and whether the programme takes online payment.

    The gateway switch is read at present time rather than stored, because it is a property
    of the programme today - a bill issued before the gateway was switched on should offer
    online payment the moment it is.
    """
    config = finance_settings(Program.TUITION.value)
    total = float(invoice.get("total_amount") or 0)
    paid = float(invoice.get("amount_paid") or 0)
    return {
        **invoice,
        "amount_outstanding": round(total - paid, 2),
        "gateway_enabled": bool(config["gateway_enabled"]),
        "gateway_provider": config["gateway_provider"],
    }


def list_invoices(student_id: int | None = None, status: str | None = None,
                  from_date=None, to_date=None) -> list[dict]:
    records = (
        firestore_tuition_invoices.query_documents("student_id", "==", student_id)
        if student_id is not None else firestore_tuition_invoices.list_all()
    )
    start = parse_date(from_date)
    end = parse_date(to_date)

    results = []
    for record in records:
        if status and record.get("status") != status:
            continue
        period_start = parse_date(record.get("period_start"))
        if start and period_start and period_start < start:
            continue
        if end and period_start and period_start > end:
            continue
        results.append(record)

    return sorted(results, key=lambda r: str(r.get("period_start") or ""), reverse=True)


def revenue_summary(from_date=None, to_date=None) -> dict:
    """
    Billed against collected over a period. The admin's one-line answer to 'how are we doing'.
    """
    invoices = list_invoices(from_date=from_date, to_date=to_date)
    live = [i for i in invoices if i.get("status") != InvoiceStatus.CANCELLED.value]
    billed = round(sum(float(i.get("total_amount") or 0) for i in live), 2)
    collected = round(sum(float(i.get("amount_paid") or 0) for i in live), 2)

    by_status: dict[str, int] = {}
    for invoice in invoices:
        by_status[str(invoice.get("status"))] = by_status.get(str(invoice.get("status")), 0) + 1

    return {
        "currency": tuition_settings()["currency"],
        "invoice_count": len(invoices),
        "total_billed": billed,
        "total_collected": collected,
        "outstanding": round(billed - collected, 2),
        "by_status": by_status,
    }


# ---------------------------------------------------------------------------------------
# The payment page
#
# `compute_breakdown` is to tuition what `app.services.finance.compute_breakdown` is to the
# school: the one place the payable is derived, rendered by the student's fee page, the
# parent's, the admin's preview, and frozen by the invoice generator. Two functions producing
# the same bill is how a preview and an invoice come to disagree.
#
# The arithmetic runs in the same order as the school's, for the same reasons:
#
#     1. line       - the classes taken in the period, priced against the package (base
#                     currency)
#     2. convert    - into the currency being displayed
#     3. discount   - the admin's manual adjustment for the period
#     4. tax        - on the discounted base, at *this currency's* rate
#     5. convenience- this currency's processing surcharge
#     6. rounding   - on the final payable only
#
# Charges are per currency, not per programme. A tuition parent paying in AED carries a
# different processing cost and a different tax position from one paying in INR, and
# `resolve_currency` holds both. See `app.services.tuition.settings_store`.
# ---------------------------------------------------------------------------------------

def _round_money(value) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _apply_rounding(amount: float, mode: str) -> float:
    """Rounds the final payable only. Rounding each line stops the lines summing to it."""
    import math

    if mode == "NEAREST":
        return float(round(amount))
    if mode == "UP":
        return float(math.ceil(amount))
    if mode == "DOWN":
        return float(math.floor(amount))
    return _round_money(amount)


def _price_in_currency(base_lines: list[dict], money_config: dict,
                       discount_amount: float = 0.0) -> dict:
    """
    Steps 2 to 6 of the arithmetic for one currency, over lines already priced in base.

    Split out of `compute_breakdown` so the same computation can run once for the currency
    being shown and once more, cheaply, for every other currency on offer. The lines are
    copied, never mutated: the base figures are the input to every run.
    """
    rate = float(money_config.get("rate_from_base") or 1.0) or 1.0

    lines = []
    for source in base_lines:
        line = dict(source)
        line["base_amount"] = source["amount"]
        line["base_unit_amount"] = source.get("unit_amount")
        line["amount"] = _round_money(source["amount"] * rate)
        if source.get("unit_amount") is not None:
            line["unit_amount"] = _round_money(source["unit_amount"] * rate)
        for key in ("package_amount", "overage_amount"):
            if source.get(key) is not None:
                line[f"base_{key}"] = source[key]
                line[key] = _round_money(source[key] * rate)

        # A tuition line is a teaching service; there is no fee-head catalogue here to mark
        # some lines taxable and others not, so the currency's own switch decides for all.
        line["taxable"] = bool(money_config.get("tax_enabled"))
        line["tax_amount"] = 0.0
        line["discount_amount"] = 0.0
        line["net_amount"] = line["amount"]
        lines.append(line)

    subtotal = _round_money(sum(line["amount"] for line in lines))

    # --- 3. Discount ----------------------------------------------------------------------
    discount_total = min(_round_money(float(discount_amount or 0) * rate), subtotal)
    if discount_total > 0 and subtotal > 0:
        # Spread across the lines in proportion so each line's net figure is explainable,
        # with the last line absorbing the rounding remainder - the same approach the school
        # module takes, and for the same reason.
        remaining = discount_total
        for index, line in enumerate(lines):
            if index == len(lines) - 1:
                share = remaining
            else:
                share = _round_money(discount_total * (line["amount"] / subtotal))
                remaining = _round_money(remaining - share)
            line["discount_amount"] = share

    # --- 4. Tax, on the discounted base ---------------------------------------------------
    tax_total = 0.0
    taxable_base = 0.0
    tax_inclusive = bool(money_config.get("tax_inclusive"))
    if money_config.get("tax_enabled"):
        tax_rate = float(money_config.get("tax_percent") or 0)
        for line in lines:
            net = _round_money(line["amount"] - line["discount_amount"])
            taxable_base = _round_money(taxable_base + net)
            if tax_inclusive:
                line["tax_amount"] = (
                    _round_money(net - (net / (1 + tax_rate / 100.0))) if tax_rate else 0.0
                )
            else:
                line["tax_amount"] = _round_money(net * tax_rate / 100.0)
            tax_total = _round_money(tax_total + line["tax_amount"])

    for line in lines:
        line["net_amount"] = _round_money(
            line["amount"] - line["discount_amount"]
            + (0.0 if tax_inclusive else line["tax_amount"])
        )

    # --- 5 & 6. Convenience, then rounding ------------------------------------------------
    payable = _round_money(sum(line["net_amount"] for line in lines))
    convenience = _round_money(
        payable * float(money_config.get("convenience_percent") or 0) / 100.0
        + float(money_config.get("convenience_amount") or 0)
    )
    raw_total = _round_money(payable + convenience)
    total = _apply_rounding(raw_total, money_config.get("rounding") or "NONE")

    return {
        "line_items": lines,
        "exchange_rate": rate,
        "subtotal": subtotal,
        "discount_total": discount_total,
        "taxable_base": taxable_base,
        "tax_total": tax_total,
        "convenience_total": convenience,
        "total_amount": total,
        "rounding_adjustment": _round_money(total - raw_total),
    }


def compute_breakdown(student_id: int, period_start, period_end,
                      currency: str | None = None,
                      discount_amount: float = 0.0,
                      on=None) -> dict:
    """
    What one student owes for a tuition period, fully derived, in one currency.

    One line, for the package, with the per-subject class counts inside it - "14 classes
    this period, 6 English, 5 Maths, 3 Science, at 500 a class" - plus where the student
    stands on their allowance. That is what makes a tuition bill explicable months later,
    and it is the whole basis on which the programme charges.

    `discount_amount` is the administrator's manual adjustment for the period, given in the
    **base** currency and converted like everything else; tuition has no rule-driven discount
    engine because a package's price is negotiated when it is assigned.
    """
    from app.services.tuition.settings_store import finance_settings, resolve_currency

    start = parse_date(period_start)
    end = parse_date(period_end)
    if not start or not end:
        raise HTTPException(
            status_code=400, detail="period_start and period_end are required."
        )
    if end < start:
        raise HTTPException(
            status_code=400, detail="period_end cannot be earlier than period_start."
        )

    student = firestore_users.get_document(str(student_id))
    if not student:
        raise HTTPException(status_code=404, detail=f"Student {student_id} not found")

    money_config = resolve_currency(Program.TUITION.value, currency)
    # The gateway switch is a property of the programme, not of a currency - one provider
    # serves every currency it is configured for.
    program_config = finance_settings(Program.TUITION.value)

    year = year_for_period(start)
    academic_year_id = year["id"] if year else None

    # --- 1. The package line, priced in the base currency ---------------------------------
    sessions = _student_sessions(student_id)
    line = price_package(student_id, start, end, year, sessions=sessions)
    base_lines = [line] if (line["sessions_conducted"] or line["amount"] or line["package_id"]) else []

    # --- 2 to 6. Convert, discount, tax, convenience, round - in the chosen currency -------
    priced = _price_in_currency(base_lines, money_config, discount_amount)
    lines = priced["line_items"]

    # The same arithmetic once more per offered currency, so the switcher can show what each
    # choice would come to - with that currency's own tax and surcharge - before the payer
    # picks it. Pure arithmetic over lines already priced; no further reads.
    options = []
    for option in money_config.get("options") or []:
        entry = (money_config.get("all_currencies") or {}).get(option["code"]) or option
        preview = _price_in_currency(base_lines, entry, discount_amount)
        options.append({
            **option,
            "subtotal": preview["subtotal"],
            "tax_total": preview["tax_total"],
            "convenience_total": preview["convenience_total"],
            "total_amount": preview["total_amount"],
        })

    conducted = int(line["sessions_conducted"] or 0)
    if line["package_id"]:
        detail = (
            f"{line['classes_billed']} class(es) counted across {len(line['subjects'])} "
            f"subject(s) in the period; {line['classes_used_to_date']} of "
            f"{line['classes_included']} used on '{line['package_name']}'"
            + (f" ({line['term_name']})" if line.get("term_name") else "") + "."
        )
        if line.get("note"):
            detail += " " + line["note"]
    elif conducted:
        detail = (
            f"{conducted} class(es) conducted in the period, but no package is assigned - "
            "assign one to price them."
        )
    else:
        detail = "No classes were conducted in this period."

    return {
        "student_id": int(student_id),
        "student_name": student.get("full_name"),
        "admission_number": student.get("admission_number"),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "academic_year_id": academic_year_id,
        "academic_year_name": year.get("name") if year else None,
        "term": line["term"],
        "term_name": line["term_name"],

        "package_id": line["package_id"],
        "package_name": line["package_name"],
        "assignment_id": line["assignment_id"],
        "billing_mode": line["billing_mode"],
        "classes_included": line["classes_included"],
        "classes_billed": line["classes_billed"],
        "classes_used_to_date": line["classes_used_to_date"],
        "classes_remaining": line["classes_remaining"],
        "subjects": line["subjects"],

        "currency": money_config["code"],
        "currency_symbol": money_config["symbol"],
        "base_currency": money_config["base_currency"],
        "exchange_rate": priced["exchange_rate"],
        "available_currencies": money_config["available"],
        "recommended_currency": money_config.get("recommended_currency"),
        "currency_options": options,

        "line_items": lines,
        "subtotal": priced["subtotal"],
        "discount_total": priced["discount_total"],
        "taxable_base": priced["taxable_base"],
        "tax_total": priced["tax_total"],
        "tax_label": money_config["tax_label"],
        "convenience_total": priced["convenience_total"],
        "total_amount": priced["total_amount"],
        "rounding_adjustment": priced["rounding_adjustment"],

        "sessions_conducted": conducted,
        "gateway_enabled": program_config["gateway_enabled"],
        "gateway_provider": program_config["gateway_provider"],
        "detail": detail,
    }


def package_status(student_id, on: date | None = None) -> dict:
    """
    Where a student stands on their package today: the assignment and its usage.

    What the admin's student screen and the student's own fee page show above the bill.
    Returns the shell with nulls when nothing is assigned rather than 404ing, because "no
    package yet" is the normal state of a student who was admitted this morning.
    """
    today = on or date.today()
    assignment = resolve_assignment(student_id, today)
    package = (
        firestore_tuition_packages.get_document(str(assignment["package_id"]))
        if assignment else None
    )
    year = None
    if assignment and assignment.get("academic_year_id") is not None:
        year = admission_service.firestore_academic_years.get_document(
            str(assignment["academic_year_id"])
        )
    usage = package_usage(student_id, assignment, package, today, None, year)

    return {
        "student_id": int(student_id),
        "as_of": today.isoformat(),
        "assignment": present_assignment(assignment) if assignment else None,
        "package": present_package(package) if package else None,
        **usage,
    }


def present_assignment(assignment: dict) -> dict:
    package = firestore_tuition_packages.get_document(str(assignment.get("package_id"))) or {}
    year = {}
    if assignment.get("academic_year_id") is not None:
        year = admission_service.firestore_academic_years.get_document(
            str(assignment["academic_year_id"])
        ) or {}
    student = firestore_users.get_document(str(assignment.get("student_id"))) or {}
    return {
        **assignment,
        "id": int(assignment["id"]),
        "student_id": int(assignment["student_id"]),
        "student_name": student.get("full_name"),
        "package_id": int(assignment["package_id"]),
        "package_name": package.get("name"),
        "package_amount": float(package.get("amount") or 0),
        "classes_included": int(package.get("classes_included") or 0),
        "billing_mode": package.get("billing_mode"),
        "currency": package.get("currency"),
        "academic_year_name": year.get("name"),
        "term_name": _term_name(assignment.get("term")),
        "is_active": bool(assignment.get("is_active", True)),
    }
