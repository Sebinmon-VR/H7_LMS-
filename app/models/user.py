import enum
from dataclasses import dataclass, field
from datetime import datetime


class UserRole(str, enum.Enum):
    """
    User Role Enumeration for Role-Based Access Control (RBAC).
    """
    ADMIN = "ADMIN"
    TEACHER = "TEACHER"
    STUDENT = "STUDENT"


@dataclass(slots=True)
class User:
    """
    User entity representing students, teachers, and administrators.
    """
    full_name: str
    email: str
    hashed_password: str
    role: UserRole = UserRole.STUDENT
    is_active: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)
    id: int | None = None

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email} role={self.role}>"
