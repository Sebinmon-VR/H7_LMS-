from datetime import datetime, timedelta
from typing import Any
from jose import JWTError, jwt
from passlib.context import CryptContext
from app.core.config import settings
from app.core.enums import UserRole
from app.schemas.auth import TokenData

# CryptContext configured with bcrypt algorithm for password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """
    Hashes a plain text password using bcrypt.
    """
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verifies a plain text password against a stored bcrypt hash.
    """
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(subject: str | int, role: UserRole, email: str, expires_delta: timedelta = None) -> str:
    """
    Encodes and signs a JWT access token containing user_id, email, and user role.
    """
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode: dict[str, Any] = {
        "sub": str(subject),
        "email": email,
        "role": role.value,
        "exp": expire
    }
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt


def decode_access_token(token: str) -> TokenData | None:
    """
    Decodes a JWT access token and returns payload TokenData. Returns None if invalid.
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id_str: str = payload.get("sub")
        email: str = payload.get("email")
        role_str: str = payload.get("role")

        if user_id_str is None or role_str is None:
            return None

        return TokenData(
            user_id=int(user_id_str),
            email=email,
            role=UserRole(role_str)
        )
    except (JWTError, ValueError):
        return None
