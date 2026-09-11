import enum


class UserRole(str, enum.Enum):
    """User Role Enumeration for Role-Based Access Control (RBAC)."""
    ADMIN = "ADMIN"
    CLASS_TEACHER = "CLASS_TEACHER"
    TEACHER = "TEACHER"
    STUDENT = "STUDENT"


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


class FeeBasis(str, enum.Enum):
    """
    How a tuition fee is worked out.

    PER_SESSION is the one the brief asks for: the admin's reports count conducted classes
    and the invoice multiplies. MONTHLY is a flat retainer regardless of count, and HOURLY
    bills the minutes actually taught, which differ from the scheduled minutes whenever a
    teacher was late and the class ran on.
    """
    PER_SESSION = "PER_SESSION"
    HOURLY = "HOURLY"
    MONTHLY = "MONTHLY"


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
