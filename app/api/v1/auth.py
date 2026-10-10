"""
Authentication routes.

Sign-in is owned by this backend (see app/core/identity.py): `POST /auth/login` checks the
password and returns a session token, which clients send as `Authorization: Bearer <token>`
and renew with `POST /auth/refresh` before it runs out.

There is deliberately no public registration endpoint. Accounts are provisioned by an
administrator through `POST /api/v1/admin/users`, or by the bootstrap/provisioning scripts.
Self-service registration previously accepted a `role` field from an unauthenticated request
body, which let any caller create themselves an ADMIN account.
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

from app.api.v1.dependencies import get_current_user, resolve_token
from app.core import demo, identity
from app.core.config import settings
from app.core.enums import UserRole
from app.schemas.auth import (
    LoginRequest, PasswordChangeRequest, PasswordResetConfirm, PasswordResetRequest, Token,
)
from app.schemas.user import MeOut, UserOut
from app.services import presence

logger = logging.getLogger("auth_routes")

router = APIRouter(prefix="/auth", tags=["Authentication"])

_bearer = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")


def _token_for(user_document: dict, auth_time: int | None = None) -> Token:
    access_token, lifetime = identity.issue_access_token(user_document, auth_time=auth_time)
    return Token(
        access_token=access_token,
        token_type="bearer",
        expires_in=lifetime,
        role=UserRole(user_document["role"]),
        user_id=int(user_document["id"]),
        full_name=user_document.get("full_name") or user_document.get("email") or "",
    )


def _sign_in(email: str, password: str) -> Token:
    if identity.is_locked_out(email or ""):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed attempts. Try again in {settings.LOGIN_LOCKOUT_MINUTES} "
                   "minutes, or reset your password.",
        )

    user_document = identity.authenticate(email, password)
    if user_document is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )
    if not user_document.get("is_active", False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user account. Your account has been deactivated; please contact the school office.")
    demo.assert_not_expired(user_document)
    return _token_for(user_document)


@router.post("/login", response_model=Token)
def login(login_data: LoginRequest):
    """Exchange an email and password for a session token."""
    return _sign_in(login_data.email, login_data.password)


@router.post("/token", response_model=Token, include_in_schema=False)
def login_form(form_data: OAuth2PasswordRequestForm = Depends()):
    """
    OAuth2 form login handler compatible with the Swagger UI authentication modal.
    """
    return _sign_in(form_data.username, form_data.password)


@router.post("/refresh", response_model=Token)
def refresh(token: str = Depends(_bearer)):
    """
    A fresh token for a session that is still valid.

    The app calls this shortly before its token runs out, so somebody working all day is not
    signed out mid-lesson. A session can be renewed for up to
    identity.SESSION_MAX_DAYS days after the password was last typed.
    """
    claims, user_document = resolve_token(token)
    if not user_document.get("is_active", False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user account. Your account has been deactivated; please contact the school office.")
    demo.assert_not_expired(user_document)
    if not identity.may_refresh(claims):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your session is too old to renew. Sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _token_for(user_document, auth_time=claims.get("auth_time"))


@router.post("/password/change")
def change_password(payload: PasswordChangeRequest, current_user: UserOut = Depends(get_current_user)):
    """
    Change your own password. Every other signed-in device is signed out; the token returned
    here keeps this one signed in.
    """
    from app.core.firebase import firestore_users

    user_document = identity.authenticate(current_user.email, payload.current_password)
    if user_document is None or int(user_document["id"]) != int(current_user.id):
        raise HTTPException(status_code=400, detail="Your current password is not right.")
    problem = identity.password_problem(payload.new_password)
    if problem:
        raise HTTPException(status_code=400, detail=problem)

    identity.set_password(current_user.id, payload.new_password, source="changed")
    refreshed = firestore_users.get_document(str(current_user.id)) or user_document
    return {"detail": "Password changed.", "token": _token_for(refreshed).model_dump()}


@router.post("/password/reset-request")
def request_password_reset(payload: PasswordResetRequest, background: BackgroundTasks):
    """
    Email a password reset link.

    Always answers the same way, whether or not the address has an account, so this cannot
    be used to find out who studies here.
    """
    from app.core import mailer
    from app.core.firebase import firestore_users

    email = (payload.email or "").strip()
    answer = {
        "detail": "If that address has an account, a link to reset its password is on its way. "
                  f"It works for {settings.PASSWORD_RESET_TOKEN_MINUTES} minutes.",
    }
    user_document = firestore_users.get_document_by_field("email", email.lower())         or firestore_users.get_document_by_field("email", email)
    if not user_document or not user_document.get("is_active", False):
        return answer

    link = identity.reset_link(identity.issue_reset_token(user_document))
    if not link:
        logger.error("Password reset for %s not sent: set LMS_APP_URL so the link can be built.", email)
        return answer
    background.add_task(
        mailer.send_password_reset_email,
        user_document["email"], user_document.get("full_name") or user_document["email"], link,
    )
    return answer


@router.post("/password/reset")
def reset_password(payload: PasswordResetConfirm):
    """Set a new password from a reset link. The link stops working once it has been used."""
    user_document = identity.user_for_reset_token(payload.token)
    if user_document is None:
        raise HTTPException(
            status_code=400,
            detail="This reset link has expired or was already used. Ask for a new one.",
        )
    problem = identity.password_problem(payload.new_password)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    identity.set_password(user_document["id"], payload.new_password, source="reset")
    return {"detail": "Password changed. Sign in with your new password."}


@router.get("/me", response_model=MeOut)
def get_current_user_profile(current_user: UserOut = Depends(get_current_user)):
    """
    Get details of the currently logged in user profile.

    Carries `school_timezone` alongside the profile: the timetable's times are wall-clock
    values in that zone, and the client needs to know which zone before it can show them
    to somebody whose own clock differs.
    """
    from app.services.timetable import school_timezone_name

    return MeOut(**current_user.model_dump(), school_timezone=school_timezone_name())


@router.post("/heartbeat")
def heartbeat(current_user: UserOut = Depends(get_current_user)):
    """
    A sign of life from an open tab.

    The frontend sends one a minute while a page is visible, so somebody reading without
    clicking still shows as online to the office. Every authenticated request counts as
    well; this is for the quiet stretches. Resolving the user already recorded the touch.
    """
    return {"ok": True, "online_window_seconds": presence.ONLINE_WINDOW_SECONDS}
