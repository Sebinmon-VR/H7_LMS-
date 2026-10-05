from pydantic import BaseModel, Field
from app.core.enums import UserRole


class LoginRequest(BaseModel):
    """
    JSON Schema for authenticating users via Email and Password.
    """
    email: str
    password: str


class Token(BaseModel):
    """
    Response schema returning the session token and user metadata.
    """
    access_token: str
    token_type: str = "bearer"
    # Seconds until the token runs out; renew it with POST /auth/refresh before then.
    expires_in: int | None = None
    role: UserRole
    user_id: int
    full_name: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=1)


class PasswordResetRequest(BaseModel):
    email: str


class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str = Field(..., min_length=1)
