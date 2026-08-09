"""
Authentication routes.

There is deliberately no public registration endpoint. Accounts are provisioned by an
administrator through `POST /api/v1/admin/users`, or by the bootstrap/provisioning scripts.

Self-service registration previously accepted a `role` field from an unauthenticated
request body, which let any caller create themselves an ADMIN account. Account creation is
now an administrative action only, and signing in with Google grants no role by itself: a
Google account with no LMS profile is rejected with 403 rather than being given a default.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.v1.dependencies import get_current_user
from app.core.config import settings
from app.core.enums import UserRole
from app.core.firebase import firestore_users
from app.core.firebase_auth import sign_in_with_password
from app.schemas.auth import Token, LoginRequest
from app.schemas.user import UserOut

router = APIRouter(prefix="/auth", tags=["Authentication"])


def _issue_firebase_token(email: str, password: str) -> Token:
    """
    Exchanges an email and password for a Firebase ID token and pairs it with the LMS profile.

    Shared by the two dev-only login routes. Real clients sign in with the Firebase SDK and
    never send passwords here.
    """
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect email or password",
    )

    if not settings.FIREBASE_WEB_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="Password login is not configured on this server. "
                   "Sign in with the Firebase SDK and send the ID token as a bearer token, "
                   "or set FIREBASE_WEB_API_KEY to enable this helper.",
        )

    sign_in = sign_in_with_password(email, password)
    if not sign_in or not sign_in.get("idToken"):
        raise unauthorized

    user_document = firestore_users.get_document_by_field("email", email)
    if not user_document:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No LMS profile exists for this account. Ask an administrator to create it.",
        )

    if not user_document.get("is_active", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user account",
        )

    return Token(
        access_token=sign_in["idToken"],
        token_type="bearer",
        role=UserRole(user_document["role"]),
        user_id=int(user_document["id"]),
        full_name=user_document["full_name"],
    )


@router.post("/login", response_model=Token)
def login(login_data: LoginRequest):
    """
    Development helper: exchange email and password for a Firebase ID token.

    Production clients should authenticate with the Firebase SDK (email/password or Google
    sign-in) and send the resulting ID token as `Authorization: Bearer <idToken>`.
    """
    return _issue_firebase_token(login_data.email, login_data.password)


@router.post("/token", response_model=Token, include_in_schema=False)
def login_form(form_data: OAuth2PasswordRequestForm = Depends()):
    """
    OAuth2 form login handler compatible with the Swagger UI authentication modal.
    """
    return _issue_firebase_token(form_data.username, form_data.password)


@router.get("/me", response_model=UserOut)
def get_current_user_profile(current_user: UserOut = Depends(get_current_user)):
    """
    Get details of the currently logged in user profile.
    """
    return current_user
