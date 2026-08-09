"""
Legacy token support.

Authentication is handled by Firebase Auth (see app/core/firebase_auth.py). This module
remains only to validate JWTs that this backend issued before that migration, so tokens
already in the wild keep working during the transition. It is consulted only while
settings.ALLOW_LEGACY_JWT_LOGIN is True.

Password hashing helpers were removed deliberately: Firebase owns credentials now, and this
backend no longer stores passwords. Once ALLOW_LEGACY_JWT_LOGIN is switched off for good,
this module and the python-jose dependency can be deleted.
"""

from jose import JWTError, jwt

from app.core.config import settings
from app.core.enums import UserRole
from app.schemas.auth import TokenData


def decode_access_token(token: str) -> TokenData | None:
    """
    Decodes a legacy backend-issued JWT and returns its payload. Returns None if invalid.
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
