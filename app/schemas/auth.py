from pydantic import BaseModel
from app.core.enums import UserRole


class LoginRequest(BaseModel):
    """
    JSON Schema for authenticating users via Email and Password.
    """
    email: str
    password: str


class Token(BaseModel):
    """
    Response schema returning JWT bearer token and user metadata.
    """
    access_token: str
    token_type: str = "bearer"
    role: UserRole
    user_id: int
    full_name: str


class TokenData(BaseModel):
    """
    Internal payload decoded from verified JWT access tokens.
    """
    user_id: int | None = None
    email: str | None = None
    role: UserRole | None = None
