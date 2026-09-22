import enum


class UserRole(str, enum.Enum):
    """User Role Enumeration for Role-Based Access Control (RBAC)."""
    ADMIN = "ADMIN"
    CLASS_TEACHER = "CLASS_TEACHER"
    TEACHER = "TEACHER"
    STUDENT = "STUDENT"
    # A guardian's own login, linked to one or more student profiles. Deliberately a role
    # rather than a flag on a student account: a parent with two children cannot be one of
    # them, and letting a family share a child's login makes every audit trail a guess about
    # who actually pressed the button.
    PARENT = "PARENT"


# A class teacher is a teacher first. They take periods, mark attendance, own subject
# mappings, and appear on the timetable exactly like a TEACHER, so every check that asks
# "may this user own a teaching record?" has to accept both or a promoted teacher silently
# loses the job they were already doing.
#
# The role is only the label. The authority that makes the role worth having - seeing and
# correcting other teachers' records - is scoped to specific classes by the
# `class_teacher_mappings` collection, never by the role on its own. `app.services.permissions`
# is the single place that resolves it.
TEACHING_ROLES = frozenset({UserRole.TEACHER, UserRole.CLASS_TEACHER})
TEACHING_ROLE_VALUES = frozenset(role.value for role in TEACHING_ROLES)

# The same set plus ADMIN, for the checks that let an admin stand in for a teacher.
TEACHING_OR_ADMIN_VALUES = TEACHING_ROLE_VALUES | {UserRole.ADMIN.value}


class AttendanceStatus(str, enum.Enum):
    """Attendance status enumeration used for attendance records."""
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LATE = "LATE"
    EXCUSED = "EXCUSED"


class Gender(str, enum.Enum):
    """
    Gender recorded on a user profile.

    UNDISCLOSED exists so the field can be present on a form without forcing an answer;
    it is the value used when a profile is created without one.
    """
    MALE = "MALE"
    FEMALE = "FEMALE"
    OTHER = "OTHER"
    UNDISCLOSED = "UNDISCLOSED"


class DayOfWeek(str, enum.Enum):
    """
    Day a timetable period recurs on.

    Named rather than numbered because timetables are read and edited by humans, and
    because ISO weekday numbering (Monday=1) versus Python's `weekday()` (Monday=0) is a
    reliable source of off-by-one bugs. `iso_weekday` does the conversion in one place.
    """
    MONDAY = "MONDAY"
    TUESDAY = "TUESDAY"
    WEDNESDAY = "WEDNESDAY"
    THURSDAY = "THURSDAY"
    FRIDAY = "FRIDAY"
    SATURDAY = "SATURDAY"
    SUNDAY = "SUNDAY"

    @property
    def iso_weekday(self) -> int:
        """1 = Monday through 7 = Sunday, matching `datetime.date.isoweekday()`."""
        return _ISO_WEEKDAYS[self]

    @classmethod
    def from_date(cls, value) -> "DayOfWeek":
        """The DayOfWeek a `date` or `datetime` falls on."""
        return _BY_ISO_WEEKDAY[value.isoweekday()]


_ISO_WEEKDAYS = {
    DayOfWeek.MONDAY: 1,
    DayOfWeek.TUESDAY: 2,
    DayOfWeek.WEDNESDAY: 3,
    DayOfWeek.THURSDAY: 4,
    DayOfWeek.FRIDAY: 5,
    DayOfWeek.SATURDAY: 6,
    DayOfWeek.SUNDAY: 7,
}

_BY_ISO_WEEKDAY = {iso: day for day, iso in _ISO_WEEKDAYS.items()}


class ExamMode(str, enum.Enum):
    """
    How an exam is answered.

    ONLINE  - the student answers the form in the LMS; answers are stored per question.
    OFFLINE - the student writes on paper and uploads a scan or PDF of the answer sheet.

    The mode is fixed at creation because it decides what a submission even is, and every
    later check - what may be saved, what must be uploaded, how it is valued - branches on
    it. Changing it after students have started would orphan whatever they already filed.
    """
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"


class ExamStatus(str, enum.Enum):
    """
    The lifecycle a teacher controls by hand.

    Deliberately separate from `ExamWindowState`, which is derived from the clock. A teacher
    decides whether an exam is visible at all; the clock decides whether it is answerable
    right now. Conflating the two makes "why can my class not see tomorrow's exam?" and "why
    can they still submit?" the same field, and neither question gets a clear answer.
    """
    DRAFT = "DRAFT"          # Being written. Invisible to students.
    PUBLISHED = "PUBLISHED"  # Released to the class; the time rules now apply.
    CANCELLED = "CANCELLED"  # Called off. Visible as cancelled, accepts nothing.


class ExamWindowState(str, enum.Enum):
    """
    Where the clock currently sits relative to an exam's time rules. Never stored - always
    computed, because it changes without anybody writing to the database.
    """
    NOT_OPEN = "NOT_OPEN"  # Before `starts_at`.
    OPEN = "OPEN"          # Inside the window; submissions are on time.
    GRACE = "GRACE"        # Past `ends_at` but inside the upload concession; accepted, flagged late.
    CLOSED = "CLOSED"      # Past everything; nothing more is accepted.


class QuestionType(str, enum.Enum):
    """A question on the exam form."""
    MCQ = "MCQ"                    # One correct option.
    MULTI_SELECT = "MULTI_SELECT"  # Several correct options; all of them required for the mark.
    TRUE_FALSE = "TRUE_FALSE"
    SHORT_ANSWER = "SHORT_ANSWER"  # A word or a line; auto-marked on a normalized match.
    LONG_ANSWER = "LONG_ANSWER"    # An essay. Always valued by a human.
    NUMERIC = "NUMERIC"            # A number, compared within an optional tolerance.
    FILE_UPLOAD = "FILE_UPLOAD"    # The answer is an attached file (a diagram, a worked sheet).


# The types an answer key can settle without a human reading the script. LONG_ANSWER and
# FILE_UPLOAD are absent because no key can mark them, and pretending otherwise would put a
# zero on every essay the moment a key was saved.
OBJECTIVE_QUESTION_TYPES = frozenset({
    QuestionType.MCQ,
    QuestionType.MULTI_SELECT,
    QuestionType.TRUE_FALSE,
    QuestionType.SHORT_ANSWER,
    QuestionType.NUMERIC,
})
OBJECTIVE_QUESTION_VALUES = frozenset(t.value for t in OBJECTIVE_QUESTION_TYPES)

# Types whose answer is a list of option keys rather than free text.
CHOICE_QUESTION_VALUES = frozenset({
    QuestionType.MCQ.value,
    QuestionType.MULTI_SELECT.value,
    QuestionType.TRUE_FALSE.value,
})


class GradingScheme(str, enum.Enum):
    """
    What a valued script is worth, chosen when the exam is created.

    MARKS - the evaluator awards numbers, and a grade letter is derived from the bands if any
            were defined.
    GRADE - the evaluator awards a letter directly from the exam's bands, with no arithmetic.
            Used where the school reports grades only, so marks would be invented data.

    Fixed at creation because half a class valued in marks and half in letters cannot be
    combined into one result sheet, and a report card cannot total a column of letters.
    """
    MARKS = "MARKS"
    GRADE = "GRADE"


class SubmissionStatus(str, enum.Enum):
    """Where one student's script has got to."""
    IN_PROGRESS = "IN_PROGRESS"  # Started, answers saved, not handed in.
    SUBMITTED = "SUBMITTED"      # Handed in, awaiting valuation.
    EVALUATED = "EVALUATED"      # Valued; a mark or grade is recorded.
    MISSED = "MISSED"            # The window closed with nothing handed in.


# ---------------------------------------------------------------------------------------
# Online tuition
#
# The tuition system is a second product sharing one login, one user table and one set of
# infrastructure with the school LMS. Everything below exists to keep the two apart at the
# level that matters - what a signed-in user may reach, and which collections a record
# belongs to - without forking the codebase.
# ---------------------------------------------------------------------------------------


class Program(str, enum.Enum):
    """
    Which product a user, or a record, belongs to.

    A user carries a *list* of these (`programs` on the profile) rather than a single value,
    because an administrator runs both and a teacher may genuinely do both jobs. A role says
    what someone may do; a program says where they may do it, and both have to pass.

    Absent on a profile means LMS: every account that existed before tuition was added was a
    school account, and defaulting the other way would silently hand the whole school access
    to a product they never bought.
    """
    LMS = "LMS"
    TUITION = "TUITION"


DEFAULT_PROGRAMS = (Program.LMS.value,)
ALL_PROGRAM_VALUES = frozenset(p.value for p in Program)


def normalize_programs(value) -> list[str]:
    """
    Coerces whatever is stored on a profile into a clean list of program values.

    Accepts a list, a single string, or None, and drops anything unrecognized. Written
    permissively because this is read on every authenticated request: a profile hand-edited
    in the Firestore console should cost that user their tuition access at worst, never a
    500 on login.
    """
    if value is None:
        return list(DEFAULT_PROGRAMS)
    if isinstance(value, str):
        candidates = [value]
    else:
        try:
            candidates = list(value)
        except TypeError:
            return list(DEFAULT_PROGRAMS)

    resolved = []
    for item in candidates:
        text = str(item).strip().upper()
        if text in ALL_PROGRAM_VALUES and text not in resolved:
            resolved.append(text)
    return resolved or list(DEFAULT_PROGRAMS)


class TuitionEnrollmentStatus(str, enum.Enum):
    """
    The life of one (student, subject) tuition arrangement.

    PAUSED rather than deleting: a student who stops for the exam season keeps their
    history, their materials and their teacher, and the slots stop generating sessions
    without anybody having to rebuild the arrangement afterwards.
    """
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class TuitionSessionStatus(str, enum.Enum):
    """
    Where one one-to-one class has got to.

    NO_SHOW_TEACHER and NO_SHOW_STUDENT are separate from CANCELLED because the fee module
    counts them differently: a class the teacher missed is not billable to the student, and
    a class the student missed generally is. Collapsing them into one "did not happen"
    status makes that distinction unrecoverable at invoicing time.
    """
    SCHEDULED = "SCHEDULED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    NO_SHOW_TEACHER = "NO_SHOW_TEACHER"
    NO_SHOW_STUDENT = "NO_SHOW_STUDENT"


# Sessions that actually took place, for attendance percentages and billable counts.
CONDUCTED_SESSION_VALUES = frozenset({
    TuitionSessionStatus.COMPLETED.value,
    TuitionSessionStatus.NO_SHOW_STUDENT.value,
})

# Sessions that are over, one way or another. Anything not in here is still to come.
CLOSED_SESSION_VALUES = CONDUCTED_SESSION_VALUES | {
    TuitionSessionStatus.CANCELLED.value,
    TuitionSessionStatus.NO_SHOW_TEACHER.value,
}


class LibraryVisibility(str, enum.Enum):
    """
    Who a shared book, note or recording reaches.

    ENROLLMENT is the default for anything a teacher or student files against a specific
    arrangement - in a one-to-one product that means exactly two people plus the admin,
    which is the privacy expectation of a private class. SUBJECT and PROGRAM widen it
    deliberately, and are how a library is actually built.
    """
    PRIVATE = "PRIVATE"          # Only the uploader (and admins).
    ENROLLMENT = "ENROLLMENT"    # The one student and their teacher for that subject.
    SUBJECT = "SUBJECT"          # Everyone taking or teaching that subject.
    PROGRAM = "PROGRAM"          # Every tuition user. The shared library.


class LibraryApprovalStatus(str, enum.Enum):
    """
    Whether an upload may be seen by anybody but its uploader.

    Students may upload - that was a requirement - but a student cannot publish to the whole
    programme unreviewed. Teacher and admin uploads are approved on arrival; a student's are
    PENDING until a teacher or admin says otherwise, except when they are PRIVATE, which
    reaches nobody and so needs no review.
    """
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class PackageBillingMode(str, enum.Enum):
    """
    How a tuition package turns into a bill.

    A package is "so much for so many classes" - 15,000 for 30 classes, any subjects. The two
    modes differ in *when* that money is asked for, not in what it buys:

      * PER_CLASS - each invoice period bills the classes the student actually attended in
                    it, at the package's per-class rate (amount / classes_included). What the
                    brief calls "bill based on the total classes attended". The package's
                    class count is the allowance the usage is reported against.
      * PACKAGE   - the whole package amount is billed once per term, on the first invoice
                    raised in it; later invoices in the same term carry the usage and bill
                    only classes beyond the allowance, at the per-class rate.
    """
    PER_CLASS = "PER_CLASS"
    PACKAGE = "PACKAGE"


class InvoiceStatus(str, enum.Enum):
    """
    An invoice's life. DRAFT is recomputed from session counts every time it is regenerated;
    once ISSUED the numbers are frozen, because a bill that changes after it was sent is not
    a bill.
    """
    DRAFT = "DRAFT"
    ISSUED = "ISSUED"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    CANCELLED = "CANCELLED"


# ---------------------------------------------------------------------------------------
# Families
#
# A school bills a household, not a pupil. Two siblings are two students everywhere else in
# this codebase, and correctly so - they sit in different classes, take different exams and
# have their own attendance. They become one thing only when money is involved, which is why
# the family unit lives here as its own concept rather than as a field on a student.
# ---------------------------------------------------------------------------------------


class GuardianRelation(str, enum.Enum):
    """
    How a parent account relates to a student it can see.

    Recorded because "who may I talk to about this child?" is a question the front office
    asks constantly, and because a legal guardian and an elder brother who does the school
    run are both valid links with very different standing.
    """
    FATHER = "FATHER"
    MOTHER = "MOTHER"
    GUARDIAN = "GUARDIAN"
    SIBLING = "SIBLING"
    OTHER = "OTHER"


# ---------------------------------------------------------------------------------------
# Notice board
# ---------------------------------------------------------------------------------------


class NoticePriority(str, enum.Enum):
    """
    How loudly a notice should be shown.

    URGENT exists so that "school closed tomorrow" can outrank "swimming kit reminder" in a
    list sorted by recency, which is the one ordering a notice board cannot get away with on
    its own.
    """
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    URGENT = "URGENT"


class NoticeStatus(str, enum.Enum):
    """
    The lifecycle an author controls.

    Separate from whether the notice's publish time has arrived, for the same reason exams
    keep `ExamStatus` and `ExamWindowState` apart: one is a decision, the other is the clock,
    and a single field cannot answer "why can nobody see this?" for both causes.
    """
    DRAFT = "DRAFT"          # Being written. Reaches nobody.
    PUBLISHED = "PUBLISHED"  # Released; the scheduling window now applies.
    ARCHIVED = "ARCHIVED"    # Taken down deliberately, kept for the record.


class NoticeAudience(str, enum.Enum):
    """
    Who a notice is addressed to.

    CLASS is the case the brief actually asks for - "notice board / notification classwise" -
    and the rest exist because a board that can only address classes cannot announce a staff
    meeting or a fee deadline. The audience decides which of the target lists on the notice
    is read; the others are ignored rather than combined, so a notice always has exactly one
    answer to "who gets this?".
    """
    EVERYONE = "EVERYONE"  # Every active user in the program.
    ROLE = "ROLE"          # Everyone holding one of `target_roles`.
    CLASS = "CLASS"        # Every student enrolled in one of `target_class_ids`,
                           # plus the teachers who take those classes.
    USER = "USER"          # Exactly the profiles in `target_user_ids`.


# ---------------------------------------------------------------------------------------
# Admissions
# ---------------------------------------------------------------------------------------


class IdentifierMode(str, enum.Enum):
    """
    Whether the system invents admission and staff numbers, or the office types them in.

    Both are legitimate. A new school wants them generated; a school migrating twenty years
    of paper records has numbers already and being handed different ones is an error, not a
    feature. AUTO issues one only when the field was left blank, so a mixed intake works
    without switching the setting back and forth.
    """
    AUTO = "AUTO"
    MANUAL = "MANUAL"


class AcademicYearStatus(str, enum.Enum):
    """
    Where a session year sits relative to today.

    Stored rather than derived from the dates, because the school's answer and the calendar's
    answer legitimately differ: results, transfers and fee arrears keep a year open for weeks
    after its last teaching day, and admissions for the next one open long before it starts.
    """
    UPCOMING = "UPCOMING"    # Taking admissions; not yet teaching.
    ACTIVE = "ACTIVE"        # The year currently being taught.
    CLOSED = "CLOSED"        # Over, and settled. Read-only.


class AcademicTerm(str, enum.Enum):
    """
    The two halves of a session year.

    The year runs April to March and is billed in two terms: Term 1 from April to the end of
    October, Term 2 from November to the end of March. Held as an enum rather than free text
    because a package, an instalment and an invoice all have to agree on which half they mean,
    and "Term I" and "Term 1" are the same term to a person and different keys to a query.
    """
    TERM_1 = "TERM_1"
    TERM_2 = "TERM_2"


# The calendar the terms are cut from. A session year starts in this month and the second
# term starts in `TERM_2_START_MONTH`; `app.services.admissions.default_terms` derives the
# actual dates for a given year from these two numbers, so changing the school's calendar is
# a two-constant edit rather than a hunt through the fee code.
ACADEMIC_YEAR_START_MONTH = 4   # April
TERM_2_START_MONTH = 11         # November

TERM_NAMES = {
    AcademicTerm.TERM_1.value: "Term 1",
    AcademicTerm.TERM_2.value: "Term 2",
}


# ---------------------------------------------------------------------------------------
# Finance
#
# The school side of money, alongside the tuition fee types already defined above. They stay
# separate for the reason the teaching collections do: tuition bills a count of classes that
# actually happened, while a school bills a structure agreed in advance and collects it in
# instalments. One set of types covering both would have to make every field optional, and an
# invoice where nothing is required explains nothing.
# ---------------------------------------------------------------------------------------


class FeeFrequency(str, enum.Enum):
    """
    How often a fee head is charged.

    ONE_TIME is the admission charge and anything else billed once at joining; it is the
    reason a fee structure cannot simply be "an annual amount divided by twelve".
    """
    ONE_TIME = "ONE_TIME"
    MONTHLY = "MONTHLY"
    TERM = "TERM"
    ANNUAL = "ANNUAL"


class ChargeKind(str, enum.Enum):
    """
    What a non-tuition amount on a bill actually is.

    Kept as an enum rather than free text because each kind is treated differently by the
    arithmetic: a DISCOUNT subtracts, a TAX is computed on a base rather than added flat, and
    a LATE_FEE must not itself be taxed. A string field would leave that logic guessing.
    """
    FEE = "FEE"                  # The teaching charge itself.
    CHARGE = "CHARGE"            # Transport, lab, exam entry - anything billed alongside.
    TAX = "TAX"                  # Computed from a taxable base, never entered flat.
    CONVENIENCE = "CONVENIENCE"  # Payment-processing surcharge.
    LATE_FEE = "LATE_FEE"        # Penalty for a missed instalment date.
    DISCOUNT = "DISCOUNT"        # Subtracts. Negative amounts are never used; the kind says it.
    OTHER = "OTHER"


class DiscountBasis(str, enum.Enum):
    """
    Why a concession applies, which decides what evidence resolves it.

    SIBLING and MULTI_REGISTRATION are the two the brief names, and they are genuinely
    different rules: the first counts children in a household, the second counts enrollments
    held by one student at the same time. A school that collapses them gives a two-subject
    student the family discount, which is not what anybody agreed to.
    """
    SIBLING = "SIBLING"                      # Nth child of a household pays less.
    MULTI_REGISTRATION = "MULTI_REGISTRATION"  # One student, several concurrent enrollments.
    CATEGORY = "CATEGORY"                    # Comes with the admission category.
    SCHOLARSHIP = "SCHOLARSHIP"              # Awarded to a named student.
    EARLY_PAYMENT = "EARLY_PAYMENT"          # Settled before a date.
    MANUAL = "MANUAL"                        # An admin's one-off decision, with a reason.


class DiscountValueType(str, enum.Enum):
    """Whether a concession is a percentage of the base or a flat sum off."""
    PERCENT = "PERCENT"
    AMOUNT = "AMOUNT"


class InstalmentStatus(str, enum.Enum):
    """
    Where one instalment of a bill has got to.

    OVERDUE is derived from the clock at read time and never stored - storing it would mean
    an instalment only became overdue when some sweep happened to run, and a fee report that
    is wrong between midnight and the sweep is a fee report nobody trusts.
    """
    PENDING = "PENDING"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    OVERDUE = "OVERDUE"
    WAIVED = "WAIVED"


class PaymentMethod(str, enum.Enum):
    """
    How money arrived.

    ONLINE is the gateway; everything else is recorded by the office after the fact. The
    distinction matters because an ONLINE payment carries a provider reference that can be
    reconciled, and the others carry whatever the clerk typed.
    """
    CASH = "CASH"
    BANK_TRANSFER = "BANK_TRANSFER"
    CHEQUE = "CHEQUE"
    CARD = "CARD"
    UPI = "UPI"
    ONLINE = "ONLINE"
    ADJUSTMENT = "ADJUSTMENT"  # A correction, not money: a write-off or a balance transfer.
    OTHER = "OTHER"


class GatewayMethod(str, enum.Enum):
    """
    How the payer *wants* to pay, chosen on the checkout page.

    Distinct from `PaymentMethod`, which records how money actually arrived: a payer who
    picks UPI and abandons the page has paid by nothing. The first four are what a gateway
    offers and are handed to the adapter as a hint; the last two are the offline routes,
    kept on the same intent so the office can match a bank transfer or a counter payment to
    the reference the payer was shown.
    """
    UPI = "UPI"
    CARD = "CARD"
    NET_BANKING = "NET_BANKING"
    WALLET = "WALLET"
    BANK_TRANSFER = "BANK_TRANSFER"
    OFFICE = "OFFICE"


OFFLINE_GATEWAY_METHODS = frozenset({
    GatewayMethod.BANK_TRANSFER.value,
    GatewayMethod.OFFICE.value,
})


class PaymentIntentStatus(str, enum.Enum):
    """
    A gateway payment's life, as this system sees it.

    Deliberately provider-neutral. No gateway is wired yet - the brief is to show the fees,
    charges and taxes now and integrate a provider later - so these are the states every
    provider agrees on, and the adapter that arrives later maps its own vocabulary onto them
    rather than this enum being rewritten to match whichever one is chosen.

    REQUIRES_ACTION covers 3-D Secure and UPI collect requests: the payer has more to do and
    the intent is neither pending on our side nor finished.
    """
    CREATED = "CREATED"
    REQUIRES_ACTION = "REQUIRES_ACTION"
    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"


# Intent states that are over. Anything else may still change, and a reconciliation sweep
# has to keep asking the provider about it.
TERMINAL_INTENT_STATES = frozenset({
    PaymentIntentStatus.SUCCEEDED.value,
    PaymentIntentStatus.FAILED.value,
    PaymentIntentStatus.CANCELLED.value,
    PaymentIntentStatus.REFUNDED.value,
})


# ---------------------------------------------------------------------------------------
# Approval workflows
#
# Extra classes, staff leave and support tickets are three different subjects sharing one
# shape: somebody asks, somebody decides, and the record of both survives. They keep separate
# status enums rather than one `ApprovalStatus` because the terminal states genuinely differ -
# a rejected leave request is not a closed support ticket - and a shared enum would carry
# values that are meaningless in two of the three places it is used.
# ---------------------------------------------------------------------------------------


class ExtraClassStatus(str, enum.Enum):
    """
    A request to hold a class outside the timetable.

    CANCELLED is distinct from REJECTED because who withdrew it matters: a teacher who no
    longer needs the slot and an administrator who refused it produce the same empty
    timetable and very different conversations.

    SCHEDULED is the state after approval, once the meeting or session actually exists. The
    approval and the creation are separate steps because the creation can fail - a Meet link,
    a timetable clash - and an approved request whose class was never made must stay visible
    rather than silently reverting to pending.
    """
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    SCHEDULED = "SCHEDULED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class LeaveType(str, enum.Enum):
    """
    The kind of absence, which decides what it costs the teacher.

    Recorded per request because a school's leave policy counts these separately - casual
    leave against an annual allowance, sick leave usually not - and a single "leave" record
    makes the balance impossible to compute afterwards.
    """
    CASUAL = "CASUAL"
    SICK = "SICK"
    EARNED = "EARNED"
    UNPAID = "UNPAID"
    MATERNITY = "MATERNITY"
    BEREAVEMENT = "BEREAVEMENT"
    OTHER = "OTHER"


class LeaveStatus(str, enum.Enum):
    """
    Where a leave application has got to.

    WITHDRAWN is the applicant's own action and is kept apart from REJECTED for the same
    reason ExtraClassStatus separates them: the timetable consequence is identical and the
    human one is not.
    """
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"
    CANCELLED = "CANCELLED"


class LeaveDayPart(str, enum.Enum):
    """
    How much of a day a leave request covers.

    Half days exist because they are most of what a school actually grants - a teacher
    leaving after lunch for an appointment - and rounding them up to a full day makes every
    leave balance wrong in the teacher's favour, which is the version nobody checks.
    """
    FULL_DAY = "FULL_DAY"
    FIRST_HALF = "FIRST_HALF"
    SECOND_HALF = "SECOND_HALF"


class TicketStatus(str, enum.Enum):
    """
    A support ticket's life.

    WAITING_ON_USER is separate from OPEN because a ticket blocked on the person who raised
    it should not count against the support team's response time, and a queue that cannot
    express "we asked them a question three days ago" measures the wrong thing.
    """
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING_ON_USER = "WAITING_ON_USER"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class TicketPriority(str, enum.Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    URGENT = "URGENT"


class TicketCategory(str, enum.Enum):
    """
    What a ticket is about, used to route it.

    Deliberately coarse. A long list looks thorough and is filled in wrongly; six categories
    that map to who actually handles the problem are more useful than twenty that map to how
    the reporter described it.
    """
    TECHNICAL = "TECHNICAL"      # Login, video, uploads - anything that is broken.
    ACADEMIC = "ACADEMIC"        # Classes, marks, timetable.
    BILLING = "BILLING"          # Fees, invoices, payments.
    ACCOUNT = "ACCOUNT"          # Profile, credentials, access.
    FEEDBACK = "FEEDBACK"
    OTHER = "OTHER"


# Ticket states that still need somebody's attention, for the admin's open-queue count.
ACTIVE_TICKET_STATES = frozenset({
    TicketStatus.OPEN.value,
    TicketStatus.IN_PROGRESS.value,
    TicketStatus.WAITING_ON_USER.value,
})


class HomeworkStatus(str, enum.Enum):
    """
    Where one student's homework has got to.

    MISSED is stamped by the clock rather than by a teacher, so a daily-homework report is
    answerable the morning after rather than whenever somebody gets round to marking.
    """
    ASSIGNED = "ASSIGNED"
    SUBMITTED = "SUBMITTED"
    GRADED = "GRADED"
    MISSED = "MISSED"
