"""
Firebase Authentication integration.

Login, password storage, resets, and session revocation are delegated to Firebase Auth.
The backend never stores passwords; it only verifies the ID tokens clients present and
keeps a Firestore user profile linked by `firebase_uid`.

Every helper degrades gracefully when the Admin SDK is unavailable (missing credentials,
offline dev) so the API stays importable and testable without a live Firebase project.
"""

import logging

import httpx

from app.core.config import settings
from app.core.enums import UserRole
from app.core.firebase import initialize_firebase

logger = logging.getLogger("firebase_auth")

IDENTITY_TOOLKIT_SIGNIN_URL = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
)


def _auth_module():
    """
    Returns the firebase_admin.auth module once the default app is initialized,
    or None when the SDK is unavailable.
    """
    if initialize_firebase() is None:
        return None
    try:
        from firebase_admin import auth
        return auth
    except ModuleNotFoundError as exc:
        logger.warning("firebase_admin.auth unavailable: %s", exc)
        return None


def is_available() -> bool:
    """True when Firebase Auth operations can actually be performed."""
    return _auth_module() is not None


def verify_token(id_token: str) -> dict | None:
    """
    Verifies a Firebase ID token's signature, expiry, and audience.

    Returns the decoded claims (including `uid`, `email`, and any custom claims),
    or None when the token is invalid, expired, or revoked.
    """
    auth = _auth_module()
    if auth is None:
        return None

    try:
        return auth.verify_id_token(id_token, check_revoked=True)
    except Exception as exc:
        # Covers ExpiredIdTokenError, RevokedIdTokenError, InvalidIdTokenError, and
        # transient certificate-fetch failures. All mean "not authenticated".
        logger.debug("Firebase ID token rejected: %s", exc)
        return None


def get_auth_user_by_email(email: str):
    """Returns the Firebase Auth user record for an email, or None if absent."""
    auth = _auth_module()
    if auth is None:
        return None

    try:
        return auth.get_user_by_email(email)
    except Exception:
        return None


def create_auth_user(email: str, password: str | None, display_name: str | None = None) -> str | None:
    """
    Creates a Firebase Auth user and returns its UID.

    If the email is already registered (common when migrating, or when a user signed in
    with Google before an admin created their profile), the existing account is linked
    instead of raising a duplicate error.
    """
    auth = _auth_module()
    if auth is None:
        logger.warning("Firebase Auth unavailable; user '%s' created without an auth account.", email)
        return None

    existing = get_auth_user_by_email(email)
    if existing is not None:
        logger.info("Linking existing Firebase Auth account for '%s'.", email)
        return existing.uid

    try:
        kwargs = {"email": email, "email_verified": False}
        if password:
            kwargs["password"] = password
        if display_name:
            kwargs["display_name"] = display_name

        user_record = auth.create_user(**kwargs)
        return user_record.uid
    except Exception as exc:
        logger.error("Failed to create Firebase Auth user for '%s': %s", email, exc)
        return None


def set_auth_password(uid: str, password: str) -> bool:
    """
    Overwrites the password on an existing Firebase Auth account.

    Used when an admin issues or re-issues credentials. Any active sessions keep working
    until the caller also revokes refresh tokens.
    """
    auth = _auth_module()
    if auth is None or not uid:
        return False

    try:
        auth.update_user(uid, password=password)
        return True
    except Exception as exc:
        logger.error("Failed to set password for uid '%s': %s", uid, exc)
        return False


def delete_auth_user(uid: str) -> bool:
    """
    Permanently removes a Firebase Auth account.
    Used to roll back when the Firestore profile write fails after account creation,
    so a retry is not blocked by a duplicate-email error.
    """
    auth = _auth_module()
    if auth is None or not uid:
        return False

    try:
        auth.delete_user(uid)
        return True
    except Exception as exc:
        logger.error("Failed to delete Firebase Auth user '%s': %s", uid, exc)
        return False


def set_role_claims(uid: str, role: UserRole | str, lms_user_id: int) -> bool:
    """
    Writes the LMS role and numeric user id into Firebase custom claims.

    This lets the frontend gate UI straight from the ID token without an extra API call.
    The Firestore profile document remains the authoritative source for the backend.
    """
    auth = _auth_module()
    if auth is None or not uid:
        return False

    role_value = role.value if isinstance(role, UserRole) else str(role)
    try:
        auth.set_custom_user_claims(uid, {"role": role_value, "lms_user_id": lms_user_id})
        return True
    except Exception as exc:
        logger.error("Failed to set custom claims for uid '%s': %s", uid, exc)
        return False


def set_user_disabled(uid: str, disabled: bool) -> bool:
    """Disables or re-enables a Firebase Auth account, blocking or restoring sign-in."""
    auth = _auth_module()
    if auth is None or not uid:
        return False

    try:
        auth.update_user(uid, disabled=disabled)
        return True
    except Exception as exc:
        logger.error("Failed to set disabled=%s for uid '%s': %s", disabled, uid, exc)
        return False


def revoke_tokens(uid: str) -> bool:
    """
    Revokes all refresh tokens for a user, forcing re-authentication.
    `verify_token` uses check_revoked=True, so existing ID tokens stop working too.
    """
    auth = _auth_module()
    if auth is None or not uid:
        return False

    try:
        auth.revoke_refresh_tokens(uid)
        return True
    except Exception as exc:
        logger.error("Failed to revoke tokens for uid '%s': %s", uid, exc)
        return False


def update_auth_user(uid: str, email: str | None = None, display_name: str | None = None) -> bool:
    """Keeps the Firebase Auth record in sync when an admin edits a user profile."""
    auth = _auth_module()
    if auth is None or not uid:
        return False

    updates = {}
    if email is not None:
        updates["email"] = email
    if display_name is not None:
        updates["display_name"] = display_name
    if not updates:
        return True

    try:
        auth.update_user(uid, **updates)
        return True
    except Exception as exc:
        logger.error("Failed to update Firebase Auth user '%s': %s", uid, exc)
        return False


def sign_in_with_password(email: str, password: str) -> dict | None:
    """
    Exchanges an email and password for a Firebase ID token via the Identity Toolkit
    REST API.

    This exists only so the Swagger UI auth modal and local scripts keep working; real
    clients should sign in with the Firebase SDK and never send passwords to this backend.
    Requires FIREBASE_WEB_API_KEY.
    """
    if not settings.FIREBASE_WEB_API_KEY:
        return None

    try:
        response = httpx.post(
            IDENTITY_TOOLKIT_SIGNIN_URL,
            params={"key": settings.FIREBASE_WEB_API_KEY},
            json={"email": email, "password": password, "returnSecureToken": True},
            timeout=10.0,
        )
        if response.status_code != 200:
            logger.debug("Identity Toolkit sign-in failed: %s", response.text)
            return None
        return response.json()
    except Exception as exc:
        logger.error("Identity Toolkit sign-in error: %s", exc)
        return None
