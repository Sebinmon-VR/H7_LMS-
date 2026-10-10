import logging
from typing import Callable, List

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer

from app.core import demo, identity
from app.core.enums import UserRole
from app.core.firebase import firestore_users
from app.schemas.user import UserOut
from app.services import presence

logger = logging.getLogger("auth_dependencies")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")


def resolve_token(token: str) -> tuple[dict, dict]:
    """
    The verified claims and the profile behind a session token, or 401.

    A token is refused when it is forged or expired, when its account is gone, and when the
    account's sessions were revoked after it was issued (a password change or reset, a
    deactivation) - see `identity.session_is_current`.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Your session has ended. Please sign in again.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    claims = identity.decode_token(token)
    if claims is None:
        raise credentials_exception
    user_document = firestore_users.get_document(str(claims["sub"]))
    if user_document is None or not identity.session_is_current(claims, user_document):
        raise credentials_exception
    return claims, user_document


def get_current_user(request: Request, token: str = Depends(oauth2_scheme)) -> UserOut:
    """
    FastAPI dependency resolving the authenticated user from a bearer token.

    The token is one this backend issued at sign-in (`POST /auth/login`). Roles are read from
    the profile on every request, never from the token, so a role change takes effect at once.

    A demo account is held to its unlocked modules and its expiry date here, on every
    request, so a locked screen stays locked whatever URL is typed (app/core/demo.py).
    """
    _, user_document = resolve_token(token)

    if not user_document.get("is_active", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            # The frontend keys on this phrase; it shows its own sentence for it.
            detail="Inactive user account. Your account has been deactivated; please contact the school office."
        )
    demo.assert_allowed(user_document, request.url.path)

    user = UserOut(**user_document)
    # Every authenticated request is a sign of life for the office's "who is online";
    # throttled inside to one write a minute per user.
    presence.touch(user)
    return user


def require_roles(allowed_roles: List[UserRole]) -> Callable:
    """
    Role-Based Access Control (RBAC) dependency factory.
    Restricts access to users matching the allowed roles.

    Roles are read from the stored profile, which is authoritative; the role inside a session
    token only mirrors it for the frontend.
    """
    def role_checker(current_user: UserOut = Depends(get_current_user)) -> UserOut:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This isn't available for your account. If you think this is a mistake, "
                       "contact the school office."
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
