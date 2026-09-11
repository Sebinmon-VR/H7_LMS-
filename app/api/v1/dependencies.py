import logging
from typing import Callable, List

from fastapi import Depends, Header, HTTPException, status
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


# ---------------------------------------------------------------------------------------
# Online tuition guards
#
# Two checks, not one, and the order matters. The role guard answers "may a teacher do
# this?"; the program guard answers "is this teacher one of ours?". A school teacher with a
# perfectly valid login and the TEACHER role has no business in the tuition timetable, and
# only the second check stops them.
#
# Implemented as a wrapper around the existing role guards rather than as new roles. Adding
# TUITION_TEACHER and TUITION_STUDENT to UserRole would have meant revisiting every
# `role in TEACHING_ROLE_VALUES` check in the codebase, and a teacher who does both jobs
# would have needed two accounts. A role says what you do; a program says where.
# ---------------------------------------------------------------------------------------

def require_tuition(role_guard: Callable) -> Callable:
    """
    Wraps a role guard so it also demands tuition programme membership, and notes where the
    caller is.

    The `X-Timezone` header is the second half of the timezone requirement. A frontend sets it
    from `Intl.DateTimeFormat().resolvedOptions().timeZone` - the browser's own view of where
    the machine is - and this records it on the profile, so a student sitting outside India
    sees their classes at their own local hour without configuring anything, and so does the
    reminder email that reaches them from a background sweep hours later.

    It is recorded, never enforced: a request with no header, or with a header a browser
    mangled, simply falls through to the user's explicit zone or the programme's. Nothing here
    can fail a request that would otherwise have worked.
    """
    def guard(
        current_user: UserOut = Depends(role_guard),
        x_timezone: str | None = Header(
            None, alias="X-Timezone",
            description="The caller's IANA timezone, e.g. 'Europe/London'. Send "
                        "Intl.DateTimeFormat().resolvedOptions().timeZone from the browser.",
        ),
    ) -> UserOut:
        from app.services.tuition.common import apply_detected_timezone, assert_tuition_access

        assert_tuition_access(current_user)
        apply_detected_timezone(current_user, x_timezone)
        return current_user

    return guard


# Admins are not program-scoped: one admin team runs both products, and an administrator
# locked out of tuition because nobody ticked a box is a support call, not a security win.
require_tuition_admin = require_admin
require_tuition_teacher = require_tuition(require_teacher)
require_tuition_student = require_tuition(require_student)
require_tuition_user = require_tuition(require_any_authenticated)
