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
