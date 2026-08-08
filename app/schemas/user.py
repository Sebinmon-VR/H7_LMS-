from datetime import datetime
from pydantic import BaseModel, ConfigDict
from app.core.enums import UserRole


class UserCreate(BaseModel):
    """
    Validation schema for registering or creating a new user (Student, Teacher, or Admin).
    """
    full_name: str
    email: str
    password: str
    role: UserRole = UserRole.STUDENT


class UserUpdate(BaseModel):
    """
    Validation schema for updating user profiles.
    """
    full_name: str | None = None
    email: str | None = None
    is_active: bool | None = None


class UserOut(BaseModel):
    """
    Response schema returning non-sensitive user metadata.
    """
    id: int
    full_name: str
    email: str
    role: UserRole
    is_active: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
