from dataclasses import dataclass, field
from datetime import datetime

# Re-exported rather than redefined. This module used to carry its own copy of UserRole,
# which meant adding a role in one file left the other silently three-valued and any
# `isinstance`/equality check across the two enums failing for no visible reason.
from app.core.enums import UserRole  # noqa: F401  (part of this module's public surface)


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
