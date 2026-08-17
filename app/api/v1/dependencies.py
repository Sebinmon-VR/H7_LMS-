import logging
from typing import Callable, List

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.core.config import settings
from app.core.enums import UserRole
from app.core.firebase import firestore_users
from app.core.firebase_auth import verify_token
from app.core.security import decode_access_token
from app.schemas.user import UserOut

logger = logging.getLogger("auth_dependencies")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")


def _resolve_user_from_firebase(claims: dict) -> dict | None:
    """
    Maps verified Firebase claims onto the Firestore user profile.

    Lookup is by `firebase_uid` first. A profile created before the Firebase migration (or
    by an admin ahead of the user's first Google sign-in) will not carry that field yet, so
    we fall back to email and backfill the link on the spot.
    """
    uid = claims.get("uid")
    email = claims.get("email")

    if uid:
        user_document = firestore_users.get_document_by_field("firebase_uid", uid)
        if user_document:
            return user_document

    if email:
        user_document = firestore_users.get_document_by_field("email", email)
        if user_document:
            if uid and not user_document.get("firebase_uid"):
                firestore_users.add_document(str(user_document["id"]), {"firebase_uid": uid})
                user_document["firebase_uid"] = uid
                logger.info("Linked Firebase uid '%s' to existing user '%s'.", uid, email)
            return user_document

    return None


def get_current_user(token: str = Depends(oauth2_scheme)) -> UserOut:
    """
    FastAPI dependency resolving the authenticated user from a bearer token.

    A Firebase ID token is tried first. While ALLOW_LEGACY_JWT_LOGIN is set, a legacy
    backend-issued JWT is also accepted, so clients can migrate to the Firebase SDK without
    a flag-day cutover. Turn the flag off once every client sends Firebase ID tokens.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials or token expired",
        headers={"WWW-Authenticate": "Bearer"},
    )

    user_document = None

    claims = verify_token(token)
    if claims is not None:
        user_document = _resolve_user_from_firebase(claims)
        if user_document is None:
            # The token is genuine but no LMS profile exists for it. An admin must create
            # the user before they can use the system.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Authenticated with Google, but no LMS profile exists for this account. "
                       "Ask an administrator to create your account.",
            )

    if user_document is None and settings.ALLOW_LEGACY_JWT_LOGIN:
        token_data = decode_access_token(token)
        if token_data is not None and token_data.user_id is not None:
            user_document = firestore_users.get_document(str(token_data.user_id))

    if user_document is None:
        raise credentials_exception

    if not user_document.get("is_active", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user account"
        )

    return UserOut(**user_document)


def require_roles(allowed_roles: List[UserRole]) -> Callable:
    """
    Role-Based Access Control (RBAC) dependency factory.
    Restricts access to users matching the allowed roles.

    Roles are read from the Firestore profile, which stays authoritative regardless of the
    authentication provider; Firebase custom claims mirror it purely for the frontend.
    """
    def role_checker(current_user: UserOut = Depends(get_current_user)) -> UserOut:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Action prohibited for role '{current_user.role.value}'. Allowed roles: {[r.value for r in allowed_roles]}"
            )
        return current_user

    return role_checker


# Role guards
require_admin = require_roles([UserRole.ADMIN])
# A class teacher is a teacher too, so every ordinary teaching endpoint admits them. What
# they may additionally reach is decided per class by app.services.permissions, not here -
# a role guard cannot express "the class they lead".
require_teacher = require_roles([UserRole.TEACHER, UserRole.CLASS_TEACHER, UserRole.ADMIN])
require_class_teacher = require_roles([UserRole.CLASS_TEACHER, UserRole.ADMIN])
require_student = require_roles([UserRole.STUDENT, UserRole.ADMIN])
require_any_authenticated = require_roles(
    [UserRole.ADMIN, UserRole.CLASS_TEACHER, UserRole.TEACHER, UserRole.STUDENT]
)
