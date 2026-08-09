import enum


class UserRole(str, enum.Enum):
    """User Role Enumeration for Role-Based Access Control (RBAC)."""
    ADMIN = "ADMIN"
    TEACHER = "TEACHER"
    STUDENT = "STUDENT"


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
