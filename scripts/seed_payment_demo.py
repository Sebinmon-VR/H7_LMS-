"""
Seeds one tuition student with a month of conducted classes, so the payment page has
something real to show: a package line with its class counts by subject, tax, a convenience
charge, a total in every offered currency, and an issued invoice with a part-payment against
it.

Written for testing the payment page against a genuinely empty tuition programme. Nothing
here is random - every figure is chosen so the arithmetic can be checked by hand. The student
is on the "Standard - 30 classes" package (15,000 INR for 30 classes, billed per class
attended, so 500 a class) for Term 1 of the April-March year:

    ENGLISH   5 classes held + 1 missed (a missed class still counts)   =   6 classes
    MATHS     5 classes held (1 cancelled, not counted)                 =   5 classes
    SCIENCE   3 classes held                                            =   3 classes
                                          14 classes x 500  = subtotal  =  7,000  INR
                                          16 of the 30 classes remain

INR (recommended): GST 18% + 2% convenience, rounded to the rupee.
AED: VAT 5% + 2.9% + 1 AED convenience.   USD: no tax, 3.5% + 0.30 convenience.

Additive and idempotent. Records are found by name, code or a deterministic id before they
are created, so running it twice changes nothing. It never deletes: the one thing it *closes*
is an already-issued zero-value invoice for the same student and period, which would
otherwise sit beside the real one and confuse the page - it is cancelled, not removed.

Usage:
    python -m scripts.seed_payment_demo                       # student@tution.com
    python -m scripts.seed_payment_demo --student someone@example.com
    python -m scripts.seed_payment_demo --dry-run             # print the plan, write nothing

Undo with `python -m scripts.reset_tuition_data --confirm --student <id>` which removes the
classes, enrollments and invoices but keeps the account.
"""

import argparse
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402
from app.core.enums import (  # noqa: E402
    AcademicTerm, AttendanceStatus, InvoiceStatus, PackageBillingMode, Program,
    TuitionSessionStatus, UserRole,
)
from app.core.firebase import (  # noqa: E402
    document_cache, firestore_subjects, firestore_tuition_enrollments,
    firestore_tuition_package_assignments, firestore_tuition_packages,
    firestore_tuition_sessions, firestore_users, initialize_firebase,
)
from app.schemas.admission import AcademicYearCreate, AdmissionCategoryCreate  # noqa: E402
from app.schemas.tuition import (  # noqa: E402
    PackageAssignmentCreate, TuitionEnrollmentCreate, TuitionPackageCreate,
)
from app.services import admissions as admission_service  # noqa: E402
from app.services.tuition import enrollments as enrollment_service  # noqa: E402
from app.services.tuition import fees as fee_service  # noqa: E402
from app.services.tuition.settings_store import save_currency_settings  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger("seed_payment_demo")

DEFAULT_STUDENT = "student@tution.com"
YEAR_NAME = "Tuition 2026-27"
YEAR_START = date(2026, 4, 1)
YEAR_END = date(2027, 3, 31)
PACKAGE_NAME = "Standard - 30 classes"
PACKAGE_AMOUNT = 15000.0
PACKAGE_CLASSES = 30
PERIOD_START = date(2026, 9, 1)
PERIOD_END = date(2026, 9, 20)

# Class times in UTC. 10:30 UTC is 16:00 in Asia/Kolkata, the programme's own zone.
SUBJECT_TIMES = {"ENGLISH": (10, 30), "MATHS": (12, 0), "SCIENCE": (13, 30)}

# (subject, day of September, status, attendance, scheduled minutes, taught minutes,
#  teacher late by, topic)
CLASSES = [
    ("ENGLISH", 1, "COMPLETED", "PRESENT", 45, 45, 0, "Reading comprehension: unseen passage"),
    ("ENGLISH", 3, "COMPLETED", "PRESENT", 45, 45, 0, "Tenses revision"),
    ("ENGLISH", 8, "COMPLETED", "LATE", 45, 38, 0, "Letter writing - formal"),
    ("ENGLISH", 10, "NO_SHOW_STUDENT", "ABSENT", 45, 0, 0, None),
    ("ENGLISH", 15, "COMPLETED", "PRESENT", 45, 45, 0, "Poetry: figures of speech"),
    ("ENGLISH", 17, "COMPLETED", "PRESENT", 45, 45, 0, "Essay: structure and planning"),
    ("ENGLISH", 22, "SCHEDULED", None, 45, 0, 0, None),
    ("ENGLISH", 24, "SCHEDULED", None, 45, 0, 0, None),

    ("MATHS", 2, "COMPLETED", "PRESENT", 60, 60, 0, "Linear equations in one variable"),
    ("MATHS", 4, "COMPLETED", "PRESENT", 60, 60, 0, "Linear equations - word problems"),
    ("MATHS", 9, "CANCELLED", None, 60, 0, 0, None),
    ("MATHS", 11, "COMPLETED", "PRESENT", 60, 60, 0, "Quadrilaterals: properties"),
    ("MATHS", 16, "COMPLETED", "PRESENT", 60, 60, 0, "Mensuration: area of trapezium"),
    ("MATHS", 18, "COMPLETED", "PRESENT", 60, 60, 0, "Mensuration: surface area"),
    ("MATHS", 23, "SCHEDULED", None, 60, 0, 0, None),

    ("SCIENCE", 5, "COMPLETED", "PRESENT", 60, 60, 0, "Force and pressure"),
    ("SCIENCE", 12, "COMPLETED", "PRESENT", 60, 75, 15, "Friction - lab worksheet"),
    ("SCIENCE", 19, "COMPLETED", "PRESENT", 60, 60, 0, "Sound: how it travels"),
]

CURRENCIES = {
    "INR": {
        "enabled": True, "rate_source": "MANUAL",
        "tax_enabled": True, "tax_percent": 18, "tax_label": "GST", "tax_inclusive": False,
        "convenience_percent": 2, "convenience_amount": 0, "rounding": "NEAREST",
    },
    "AED": {
        "enabled": True, "rate_from_base": 0.044, "rate_source": "MANUAL",
        "tax_enabled": True, "tax_percent": 5, "tax_label": "VAT", "tax_inclusive": False,
        "convenience_percent": 2.9, "convenience_amount": 1, "rounding": "NONE",
    },
    "USD": {
        "enabled": True, "rate_from_base": 0.012, "rate_source": "MANUAL",
        "tax_enabled": False, "tax_percent": 0,
        "convenience_percent": 3.5, "convenience_amount": 0.3, "rounding": "NONE",
    },
}


def _utc(day: int, hour: int, minute: int) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=timezone.utc)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


class Seeder:
    def __init__(self, student_email: str, dry_run: bool):
        self.dry_run = dry_run
        self.student_email = student_email
        self.actions: list[str] = []

    # -- helpers ------------------------------------------------------------------------

    def note(self, text: str) -> None:
        self.actions.append(text)
        print(("would: " if self.dry_run else "did:   ") + text)

    def skip(self, text: str) -> None:
        print("kept:  " + text)

    # -- people -------------------------------------------------------------------------

    def find_people(self) -> None:
        users = firestore_users.list_all()

        admins = [u for u in users if u.get("role") == UserRole.ADMIN.value]
        self.admin = next(
            (a for a in admins if a.get("email") == settings.BOOTSTRAP_ADMIN_EMAIL), None
        ) or (admins[0] if admins else None)
        if not self.admin:
            sys.exit("No administrator account exists; run the app once to bootstrap one.")

        self.student = next(
            (u for u in users if str(u.get("email", "")).lower() == self.student_email.lower()),
            None,
        )
        if not self.student:
            sys.exit(f"No account with email {self.student_email}.")
        if self.student.get("role") != UserRole.STUDENT.value:
            sys.exit(f"{self.student_email} is a {self.student.get('role')}, not a student.")
        if Program.TUITION.value not in (self.student.get("programs") or []):
            sys.exit(f"{self.student_email} has no tuition access; grant it first.")

        teachers = [
            u for u in users
            if u.get("role") in {"TEACHER", "CLASS_TEACHER"}
            and Program.TUITION.value in (u.get("programs") or [])
            and u.get("is_active", True)
        ]
        if not teachers:
            sys.exit("No tuition teacher exists; add one with POST /admin/tuition/teachers.")
        teachers.sort(key=lambda t: (t.get("email") != "tutor@tution.com", str(t.get("id"))))
        # Two different teachers when the programme has them, so the bill shows more than
        # one name; the same teacher on every line otherwise.
        self.teacher_for = {
            "ENGLISH": teachers[0],
            "MATHS": teachers[0],
            "SCIENCE": teachers[1] if len(teachers) > 1 else teachers[0],
        }

        subjects = {str(s.get("name", "")).upper(): s for s in firestore_subjects.list_all()}
        self.subject_for = {}
        for name in SUBJECT_TIMES:
            match = subjects.get(name) or next(
                (s for key, s in subjects.items() if name in key), None
            )
            if not match:
                sys.exit(f"No subject named {name} exists; create it first.")
            self.subject_for[name] = match

        print(f"student: {self.student['full_name']} ({self.student['id']})")
        print(f"admin:   {self.admin.get('email')}")
        for name, teacher in self.teacher_for.items():
            print(f"{name:8} taught by {teacher.get('full_name')} ({teacher['id']})")

    # -- admissions ---------------------------------------------------------------------

    def ensure_year_and_category(self) -> None:
        year = next(
            (y for y in admission_service.list_years(Program.TUITION.value)
             if str(y.get("name", "")).strip().lower() == YEAR_NAME.lower()),
            None,
        )
        if year:
            self.skip(f"tuition year '{YEAR_NAME}' (id {year['id']})")
        else:
            self.note(f"create tuition year '{YEAR_NAME}' ({YEAR_START} to {YEAR_END}, "
                      "current; Term 1 to October, Term 2 from November)")
            if not self.dry_run:
                year = admission_service.create_year(AcademicYearCreate(
                    name=YEAR_NAME, code="TUI2627",
                    start_date=YEAR_START, end_date=YEAR_END,
                    is_current=True, programs=[Program.TUITION],
                    notes="Seeded by scripts/seed_payment_demo.py",
                ), self.admin["id"])
        self.year = year

        categories = {
            str(c.get("code", "")).upper(): c
            for c in admission_service.list_categories(
                program=Program.TUITION.value, include_inactive=True
            )
        }
        wanted = [
            ("TUI-REG", "Regular", None, 0),
            ("TUI-SIB", "Sibling", 10.0, 1),
        ]
        self.category = None
        for code, name, discount, order in wanted:
            existing = categories.get(code)
            if existing:
                self.skip(f"admission category {name} ({code})")
            else:
                self.note(f"create admission category {name} ({code})")
                if not self.dry_run:
                    existing = admission_service.create_category(AdmissionCategoryCreate(
                        name=name, code=code, programs=[Program.TUITION],
                        default_discount_percent=discount, sort_order=order,
                    ), self.admin["id"])
            if code == "TUI-REG":
                self.category = existing

        updates = {}
        if self.year and self.student.get("academic_year_id") != self.year["id"]:
            updates["academic_year_id"] = int(self.year["id"])
        if self.category and self.student.get("admission_category_id") != self.category["id"]:
            updates["admission_category_id"] = int(self.category["id"])
        if updates or (self.dry_run and not (self.year and self.category)):
            self.note(f"map student into '{YEAR_NAME}' as Regular")
            if not self.dry_run:
                firestore_users.add_document(str(self.student["id"]), updates)
        else:
            self.skip("student already mapped into the tuition year and category")

    # -- fees ---------------------------------------------------------------------------

    def ensure_package(self) -> None:
        packages = {str(p.get("name", "")).strip().lower(): p
                    for p in firestore_tuition_packages.list_all()}
        package = packages.get(PACKAGE_NAME.lower())
        if package:
            self.skip(f"package '{PACKAGE_NAME}' (id {package['id']})")
        else:
            self.note(f"create package '{PACKAGE_NAME}': {PACKAGE_AMOUNT:,.0f} INR for "
                      f"{PACKAGE_CLASSES} classes, billed per class attended")
            if not self.dry_run:
                package = fee_service.create_package(TuitionPackageCreate(
                    name=PACKAGE_NAME, amount=PACKAGE_AMOUNT,
                    classes_included=PACKAGE_CLASSES, currency="INR",
                    billing_mode=PackageBillingMode.PER_CLASS,
                    notes="Seeded by scripts/seed_payment_demo.py",
                ), self.admin)
        document_cache.invalidate(firestore_tuition_packages.collection_name)
        self.package = package

        student_id = int(self.student["id"])
        current = fee_service.resolve_assignment(student_id, PERIOD_START)
        if current and package and int(current.get("package_id")) == int(package["id"]):
            self.skip(f"student already on '{PACKAGE_NAME}' for the period "
                      f"(assignment {current['id']})")
            return
        self.note(f"assign '{PACKAGE_NAME}' to the student for Term 1 of '{YEAR_NAME}'")
        if not self.dry_run and package and self.year:
            fee_service.assign_package(student_id, PackageAssignmentCreate(
                package_id=int(package["id"]),
                academic_year_id=int(self.year["id"]),
                term=AcademicTerm.TERM_1,
            ), self.admin)
            document_cache.invalidate(firestore_tuition_package_assignments.collection_name)

    def ensure_currencies(self) -> None:
        self.note("set tuition currencies: INR (GST 18% + 2%), AED (VAT 5% + 2.9% + 1), "
                  "USD (3.5% + 0.30)")
        if not self.dry_run:
            save_currency_settings(Program.TUITION.value, "INR", CURRENCIES, self.admin["id"])

    # -- classes ------------------------------------------------------------------------

    def ensure_enrollments(self) -> None:
        self.enrollment_for = {}
        for name, subject in self.subject_for.items():
            existing = enrollment_service.existing_active(
                int(self.student["id"]), int(subject["id"])
            )
            if existing:
                self.skip(f"{name} enrollment (id {existing['id']})")
                self.enrollment_for[name] = existing
                continue
            teacher = self.teacher_for[name]
            self.note(f"enroll student in {name} with {teacher.get('full_name')}")
            if not self.dry_run:
                self.enrollment_for[name] = enrollment_service.create_enrollment(
                    TuitionEnrollmentCreate(
                        student_id=int(self.student["id"]),
                        subject_id=int(subject["id"]),
                        teacher_id=int(teacher["id"]),
                        grade_level="Grade 8",
                        default_duration_minutes=45 if name == "ENGLISH" else 60,
                        start_date=date(2026, 9, 1),
                        goals="Seeded by scripts/seed_payment_demo.py",
                    ),
                    self.admin,
                )
        document_cache.invalidate(firestore_tuition_enrollments.collection_name)

    def ensure_sessions(self) -> None:
        if self.dry_run and len(self.enrollment_for) < len(SUBJECT_TIMES):
            self.note(f"create {len(CLASSES)} classes in September 2026 "
                      "(after the enrollments above exist)")
            return

        existing = {s["id"] for s in firestore_tuition_sessions.list_all()}
        created = 0
        for subject, day, status, attendance, minutes, taught, late_by, topic in CLASSES:
            enrollment = self.enrollment_for[subject]
            doc_id = f"demo_{enrollment['id']}_2026-09-{day:02d}"
            if doc_id in existing:
                continue

            hour, minute = SUBJECT_TIMES[subject]
            starts = _utc(day, hour, minute)
            ends = starts + timedelta(minutes=minutes)
            teacher = self.teacher_for[subject]

            document = {
                "enrollment_id": enrollment["id"],
                "slot_id": None,
                "student_id": int(self.student["id"]),
                "teacher_id": int(teacher["id"]),
                "subject_id": int(self.subject_for[subject]["id"]),
                "session_date": starts.date().isoformat(),
                "scheduled_start_at": _iso(starts),
                "scheduled_end_at": _iso(ends),
                "effective_end_at": _iso(ends),
                "duration_minutes": minutes,
                "status": status,
                "title": f"{subject.title()} class",
                "meeting_link": None,
                "meet_status": None,
                "is_ad_hoc": True,
                "demo_seed": True,
                "created_by": int(self.admin["id"]),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            if status == TuitionSessionStatus.COMPLETED.value:
                teacher_in = starts + timedelta(minutes=late_by)
                student_in = starts + timedelta(minutes=8 if attendance == "LATE" else 1)
                class_start = max(teacher_in, student_in)
                effective_end = ends + timedelta(minutes=late_by)
                document.update({
                    "teacher_joined_at": _iso(teacher_in),
                    "student_joined_at": _iso(student_in),
                    "class_started_at": _iso(class_start),
                    "started_at": _iso(teacher_in),
                    "effective_end_at": _iso(effective_end),
                    "ended_at": _iso(class_start + timedelta(minutes=taught)),
                    "actual_duration_minutes": float(taught),
                    "teacher_late_minutes": float(late_by),
                    "extension_minutes": float(late_by),
                    "student_late_minutes": 7.0 if attendance == "LATE" else 0.0,
                    "attendance_status": attendance,
                    "attendance_remarks": None,
                    "topic": topic,
                    "ended_early": taught < minutes,
                    "short_by_minutes": float(max(minutes - taught, 0)),
                })
            elif status == TuitionSessionStatus.NO_SHOW_STUDENT.value:
                document.update({
                    "teacher_joined_at": _iso(starts),
                    "student_joined_at": None,
                    "class_started_at": _iso(starts),
                    "started_at": _iso(starts),
                    "ended_at": _iso(starts + timedelta(minutes=15)),
                    "actual_duration_minutes": 0.0,
                    "attendance_status": AttendanceStatus.ABSENT.value,
                    "attendance_remarks": "Student did not join.",
                })
            elif status == TuitionSessionStatus.CANCELLED.value:
                document.update({
                    "cancelled_at": _iso(starts - timedelta(hours=20)),
                    "cancelled_by": int(teacher["id"]),
                    "cancellation_reason": "Teacher unwell",
                    "is_billable": False,
                })

            created += 1
            if not self.dry_run:
                firestore_tuition_sessions.add_document(doc_id, document)

        if created:
            self.note(f"create {created} classes in September 2026 "
                      f"({len(CLASSES) - created} already there)")
        else:
            self.skip(f"all {len(CLASSES)} September classes")

    # -- invoice ------------------------------------------------------------------------

    def ensure_invoice(self) -> None:
        student_id = int(self.student["id"])
        wanted_id = fee_service.invoice_id(student_id, PERIOD_START, PERIOD_END)

        # A zero-value bill already issued for this month is what the payment page showed
        # before there were any classes. Closed, not deleted, so the record stays honest.
        for invoice in fee_service.list_invoices(student_id=student_id):
            if invoice["id"] == wanted_id:
                continue
            if invoice.get("status") in {InvoiceStatus.ISSUED.value} \
                    and float(invoice.get("total_amount") or 0) == 0 \
                    and str(invoice.get("period_start", "")).startswith("2026-09"):
                self.note(f"cancel empty invoice {invoice['id']} (total 0)")
                if not self.dry_run:
                    fee_service.cancel_invoice(
                        invoice, "Superseded by the seeded September invoice"
                    )

        if self.dry_run:
            self.note(f"generate and issue invoice {wanted_id} in INR, record a 2,000 part-payment")
            return

        existing = fee_service.list_invoices(student_id=student_id)
        invoice = next((i for i in existing if i["id"] == wanted_id), None)
        if invoice and invoice.get("status") != InvoiceStatus.DRAFT.value:
            self.skip(f"invoice {wanted_id} already {invoice['status']}")
        else:
            invoice = fee_service.generate_invoice(
                student_id, PERIOD_START, PERIOD_END, self.admin,
                due_date=date(2026, 9, 30), currency="INR",
                notes="September classes. Seeded by scripts/seed_payment_demo.py",
            )
            invoice = fee_service.issue_invoice(invoice, self.admin)
            self.note(f"generate and issue invoice {wanted_id}: "
                      f"{invoice['currency']} {invoice['total_amount']}")

        if float(invoice.get("amount_paid") or 0) == 0 \
                and invoice.get("status") != InvoiceStatus.CANCELLED.value:
            invoice = fee_service.record_payment(
                invoice, self.admin, 2000.0, method="UPI", reference="DEMO-UPI-0001",
                paid_at=datetime(2026, 9, 12, 9, 5, tzinfo=timezone.utc),
            )
            self.note(f"record part-payment 2,000 by UPI -> {invoice['status']}")
        else:
            self.skip(f"invoice payments (paid {invoice.get('amount_paid')})")

    # -- result -------------------------------------------------------------------------

    def show_breakdown(self) -> None:
        if self.dry_run:
            return
        student_id = int(self.student["id"])
        print()
        print(f"Payment page for {PERIOD_START} to {PERIOD_END}:")
        for currency in (None, "AED", "USD"):
            b = fee_service.compute_breakdown(student_id, PERIOD_START, PERIOD_END, currency=currency)
            flag = " (recommended)" if b["currency"] == b["recommended_currency"] else ""
            print(f"  {b['currency']}{flag}: subtotal {b['subtotal']}, "
                  f"{b['tax_label']} {b['tax_total']}, convenience {b['convenience_total']}, "
                  f"total {b['currency_symbol']}{b['total_amount']}")
            if currency is None:
                for line in b["line_items"]:
                    print(f"     - {line['package_name']} ({line.get('term_name')}): "
                          f"{line['classes_billed']} class(es) x {line['unit_amount']} = "
                          f"{line['amount']}; {line['classes_used_to_date']} of "
                          f"{line['classes_included']} used, {line['classes_remaining']} left")
                    for subject in line.get("subjects") or []:
                        print(f"         {subject['subject_name']}: {subject['classes_counted']} "
                              f"counted (conducted {subject['sessions_conducted']}, attended "
                              f"{subject['sessions_attended']}, missed {subject['sessions_missed']})")
                print("     switcher:", ", ".join(
                    f"{o['code']} {o['total_amount']} [{o['charges_summary']}]"
                    + (" *" if o["recommended"] else "")
                    for o in b["currency_options"]
                ))

    def run(self) -> None:
        self.find_people()
        self.ensure_year_and_category()
        self.ensure_package()
        self.ensure_currencies()
        self.ensure_enrollments()
        self.ensure_sessions()
        self.ensure_invoice()
        self.show_breakdown()
        print()
        print(f"{'Planned' if self.dry_run else 'Done'}: {len(self.actions)} change(s).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--student", default=DEFAULT_STUDENT, help="Student's login email")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan, write nothing")
    args = parser.parse_args()

    # Currency symbols in the summary; a Windows console defaults to cp1252 and would fail.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    initialize_firebase()
    Seeder(args.student, args.dry_run).run()


if __name__ == "__main__":
    main()
