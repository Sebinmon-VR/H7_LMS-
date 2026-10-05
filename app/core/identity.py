"""
Sign-in, owned by this backend.

Firebase Auth held every password and issued every session until the school's Google
Workspace was retired. This module replaces it with the smallest thing that does the same
job: password hashes in the `user_credentials` collection, and session tokens this backend
signs itself with SECRET_KEY.

Three things are worth knowing before changing it:

* **Hashes live apart from profiles.** `users` documents are returned by dozens of
  endpoints, some as raw dictionaries. A hash stored on the profile would be one careless
  `return user` away from being published, so it is kept in a collection nothing returns.
* **Sessions are revocable.** Every token carries the profile's `session_version`. Bumping
  it - a password change, a deactivation, a role change - makes every token issued before
  it stop working on the next request, which is what Firebase's "revoke refresh tokens" did.
* **Imported Firebase passwords keep working.** `scripts/import_firebase_passwords.py`
  copies Firebase's own scrypt hashes here; they are checked with Firebase's algorithm and
  replaced by a native hash the first time each owner signs in. Anyone the import missed is
  checked against Firebase's REST sign-in while FIREBASE_WEB_API_KEY is set.
"""

import base64
import hashlib
import hmac
import logging
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import httpx
from jose import JWTError, jwt

from app.core.config import settings

logger = logging.getLogger("identity")

PBKDF2_ITERATIONS = 600_000
PBKDF2_PREFIX = "pbkdf2_sha256"
FIREBASE_PREFIX = "firebase_scrypt"

# The imported Firebase hashes' signature parameters, stored beside them by the import script
# (settings.FIREBASE_HASH_* override it when set).
FIREBASE_HASH_CONFIG_ID = "_firebase_hash_config"

TOKEN_ACCESS = "access"
TOKEN_RESET = "reset"

# A session may be refreshed for this long after the password was last typed. Past it the
# person signs in again, so a token lifted from a shared computer cannot be kept alive for
# ever by refreshing it.
SESSION_MAX_DAYS = 30

IDENTITY_TOOLKIT_SIGNIN_URL = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
)


def _credentials():
    from app.core.firebase import firestore_user_credentials

    return firestore_user_credentials


def _users():
    from app.core.firebase import firestore_users

    return firestore_users


def _b64decode(value: str) -> bytes:
    """Standard or URL-safe base64, padded or not - Google hands out both."""
    text = (value or "").strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(text + "=" * (-len(text) % 4))


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


# ---------------------------------------------------------------------------------------
# Password hashes
# ---------------------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """A native PBKDF2-SHA256 hash, self-describing so its cost can be raised later."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{PBKDF2_PREFIX}${PBKDF2_ITERATIONS}${_b64(salt)}${_b64(digest)}"


def firebase_hash(password_hash: str, salt: str) -> str:
    """The stored form of a hash imported from Firebase Auth."""
    return f"{FIREBASE_PREFIX}${salt}${password_hash}"


def firebase_hash_config() -> dict | None:
    """The signer key, salt separator, rounds and memory cost the imported hashes need."""
    if settings.FIREBASE_HASH_SIGNER_KEY:
        return {
            "signer_key": settings.FIREBASE_HASH_SIGNER_KEY,
            "salt_separator": settings.FIREBASE_HASH_SALT_SEPARATOR,
            "rounds": settings.FIREBASE_HASH_ROUNDS,
            "mem_cost": settings.FIREBASE_HASH_MEM_COST,
        }
    stored = _credentials().get_document(FIREBASE_HASH_CONFIG_ID)
    return stored if stored and stored.get("signer_key") else None


def firebase_scrypt(password: str, salt_b64: str, config: dict) -> bytes:
    """
    Firebase's modified scrypt: scrypt the password with salt + separator, then use the
    first 32 bytes as an AES-256-CTR key to encrypt the project's signer key. The result is
    what Firebase stores as the password hash.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=_b64decode(salt_b64) + _b64decode(config.get("salt_separator") or ""),
        n=2 ** int(config.get("mem_cost") or 14),
        r=int(config.get("rounds") or 8),
        p=1,
        maxmem=128 * 1024 * 1024,
        dklen=64,
    )
    encryptor = Cipher(algorithms.AES(derived[:32]), modes.CTR(bytes(16))).encryptor()
    return encryptor.update(_b64decode(config["signer_key"])) + encryptor.finalize()


def _verify_firebase_scrypt(password: str, salt_b64: str, hash_b64: str) -> bool:
    config = firebase_hash_config()
    if not config:
        logger.error("An imported Firebase hash cannot be checked: its hash configuration is missing.")
        return False
    return hmac.compare_digest(firebase_scrypt(password, salt_b64, config), _b64decode(hash_b64))


def verify_password(password: str, stored: str | None) -> bool:
    """Checks a password against either stored form. Never raises."""
    if not password or not stored:
        return False
    try:
        scheme, _, rest = stored.partition("$")
        if scheme == PBKDF2_PREFIX:
            iterations, salt, digest = rest.split("$")
            produced = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), _b64decode(salt), int(iterations)
            )
            return hmac.compare_digest(produced, _b64decode(digest))
        if scheme == FIREBASE_PREFIX:
            salt, digest = rest.split("$")
            return _verify_firebase_scrypt(password, salt, digest)
    except Exception as exc:  # a malformed stored value is a failed sign-in, not a 500
        logger.warning("Stored password hash could not be checked: %s", exc)
    return False


def needs_rehash(stored: str | None) -> bool:
    if not stored or not stored.startswith(PBKDF2_PREFIX + "$"):
        return True
    try:
        return int(stored.split("$")[1]) < PBKDF2_ITERATIONS
    except (IndexError, ValueError):
        return True


def get_credentials(user_id) -> dict | None:
    return _credentials().get_document(str(user_id))


def has_password(user_id) -> bool:
    return bool((get_credentials(user_id) or {}).get("password_hash"))


def store_password_hash(user_id, password_hash: str, source: str) -> None:
    _credentials().add_document(str(user_id), {
        "user_id": int(user_id),
        "password_hash": password_hash,
        "source": source,
        "password_set_at": datetime.utcnow().isoformat(),
    })


def set_password(user_id, password: str, *, source: str = "set", revoke: bool = True) -> None:
    """Stores a new password, and by default signs the account out everywhere else."""
    store_password_hash(user_id, hash_password(password), source)
    if revoke:
        revoke_sessions(user_id)


def delete_credentials(user_id) -> bool:
    try:
        return bool(_credentials().delete_document(str(user_id)))
    except Exception as exc:
        logger.error("Could not delete the stored password of user %s: %s", user_id, exc)
        return False


def revoke_sessions(user_id) -> int:
    """Invalidates every token issued to this user so far. Returns the new version."""
    user = _users().get_document(str(user_id)) or {}
    version = int(user.get("session_version") or 0) + 1
    _users().add_document(str(user_id), {"session_version": version})
    return version


def password_problem(password: str) -> str | None:
    """Why a chosen password is refused, or None."""
    if len(password or "") < settings.PASSWORD_MIN_LENGTH:
        return f"Use at least {settings.PASSWORD_MIN_LENGTH} characters."
    if password.strip() != password:
        return "The password cannot start or end with a space."
    return None


# ---------------------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------------------

# Checked against when the address has no account; see `authenticate`.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))

_failures: dict[str, deque] = {}
_failures_lock = threading.Lock()


def _recent_failures(key: str) -> deque:
    window = settings.LOGIN_LOCKOUT_MINUTES * 60
    now = time.monotonic()
    bucket = _failures.setdefault(key, deque())
    while bucket and now - bucket[0] > window:
        bucket.popleft()
    return bucket


def is_locked_out(email: str) -> bool:
    if settings.LOGIN_MAX_FAILURES <= 0:
        return False
    with _failures_lock:
        return len(_recent_failures(email.lower())) >= settings.LOGIN_MAX_FAILURES


def _note_failure(email: str) -> None:
    with _failures_lock:
        _recent_failures(email.lower()).append(time.monotonic())


def _clear_failures(email: str) -> None:
    with _failures_lock:
        _failures.pop(email.lower(), None)


def _firebase_sign_in(email: str, password: str) -> bool:
    """The migration fallback: does Firebase Auth still accept this password?"""
    if not settings.FIREBASE_WEB_API_KEY:
        return False
    try:
        response = httpx.post(
            IDENTITY_TOOLKIT_SIGNIN_URL,
            params={"key": settings.FIREBASE_WEB_API_KEY},
            json={"email": email, "password": password, "returnSecureToken": False},
            timeout=10.0,
        )
        return response.status_code == 200
    except Exception as exc:
        logger.warning("Firebase fallback sign-in unavailable: %s", exc)
        return False


def authenticate(email: str, password: str) -> dict | None:
    """
    The profile for this email and password, or None. Upgrades an imported or fallback-
    verified password to a native hash on the way through.

    Raises nothing; the caller decides what a None means to the person at the keyboard.
    Inactive accounts are returned - the caller refuses them with a clearer message than
    "wrong password".
    """
    email = (email or "").strip().lower()
    if not email or not password:
        return None

    user = _users().get_document_by_field("email", email)
    if user is None:
        # Addresses are stored as typed; try the exact spelling too before giving up.
        user = _users().get_document_by_field("email", (email or "").strip())
    if user is None:
        # Spend the same time a wrong password would, so the response time does not say
        # which addresses have an account.
        verify_password(password, _DUMMY_HASH)
        _note_failure(email)
        return None

    stored = (get_credentials(user["id"]) or {}).get("password_hash")
    if not stored and not settings.FIREBASE_WEB_API_KEY:
        verify_password(password, _DUMMY_HASH)
    if stored:
        if not verify_password(password, stored):
            _note_failure(email)
            return None
        if needs_rehash(stored):
            store_password_hash(user["id"], hash_password(password), "rehash")
    elif _firebase_sign_in(email, password):
        store_password_hash(user["id"], hash_password(password), "firebase_signin")
        logger.info("Moved the password of %s across from Firebase Auth on sign-in.", email)
    else:
        _note_failure(email)
        return None

    _clear_failures(email)
    return user


# ---------------------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------------------

# Signing keys that are published in this repository, and so sign nothing: anybody could
# mint an administrator's token with one. A deployment still carrying one gets a random
# key for the life of the process instead - every restart signs everybody out, which is
# the nudge to set SECRET_KEY properly - and says so in the log.
_PUBLIC_KEYS = frozenset({
    "lms_super_secret_development_key_change_in_production_32bytes",
    "YOUR_SUPER_SECRET_JWT_KEY_CHANGE_THIS_IN_PRODUCTION",
})
_process_key: str | None = None


def _signing_key() -> str:
    global _process_key
    key = settings.SECRET_KEY or ""
    if key and key not in _PUBLIC_KEYS and len(key) >= 32:
        return key
    if _process_key is None:
        _process_key = secrets.token_urlsafe(48)
        logger.error(
            "SECRET_KEY is missing, too short, or a value published in the repository, so it "
            "cannot sign sessions safely. Using a random key for this process: everybody is "
            "signed out on every restart until SECRET_KEY is set to a long random value."
        )
    return _process_key


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _encode(claims: dict) -> str:
    return jwt.encode(claims, _signing_key(), algorithm=settings.ALGORITHM)


def issue_access_token(user: dict, auth_time: int | None = None) -> tuple[str, int]:
    """A session token for this profile, and its lifetime in seconds."""
    now = _now()
    lifetime = max(int(settings.ACCESS_TOKEN_EXPIRE_MINUTES), 5) * 60
    claims = {
        "typ": TOKEN_ACCESS,
        "sub": str(user["id"]),
        # The names the frontend already reads from a token when /auth/me is unreachable.
        "lms_user_id": int(user["id"]),
        "user_id": str(user["id"]),
        "email": user.get("email"),
        "role": user.get("role"),
        "sv": int(user.get("session_version") or 0),
        "iat": int(now.timestamp()),
        "auth_time": int(auth_time or now.timestamp()),
        "exp": int(now.timestamp()) + lifetime,
    }
    return _encode(claims), lifetime


def decode_token(token: str, expected_type: str = TOKEN_ACCESS) -> dict | None:
    try:
        claims = jwt.decode(token, _signing_key(), algorithms=[settings.ALGORITHM])
    except JWTError:
        return None
    # Tokens issued before this module existed carry no `typ`; they were access tokens.
    if (claims.get("typ") or TOKEN_ACCESS) != expected_type:
        return None
    if claims.get("sub") is None:
        return None
    return claims


def session_is_current(claims: dict, user: dict) -> bool:
    """False once the account's sessions were revoked after this token was issued."""
    return int(claims.get("sv") or 0) == int(user.get("session_version") or 0)


def may_refresh(claims: dict) -> bool:
    auth_time = int(claims.get("auth_time") or claims.get("iat") or 0)
    return _now().timestamp() - auth_time < SESSION_MAX_DAYS * 86400


def _reset_fingerprint(user_id) -> str:
    """Changes whenever the password does, so a reset link works exactly once."""
    stored = (get_credentials(user_id) or {}).get("password_hash") or "none"
    return hashlib.sha256(stored.encode("utf-8")).hexdigest()[:24]


def issue_reset_token(user: dict) -> str:
    now = _now()
    return _encode({
        "typ": TOKEN_RESET,
        "sub": str(user["id"]),
        "fp": _reset_fingerprint(user["id"]),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.PASSWORD_RESET_TOKEN_MINUTES)).timestamp()),
    })


def user_for_reset_token(token: str) -> dict | None:
    """The account a reset link belongs to, if it is genuine, unexpired and unused."""
    claims = decode_token(token, TOKEN_RESET)
    if not claims:
        return None
    user = _users().get_document(str(claims["sub"]))
    if not user or not hmac.compare_digest(str(claims.get("fp")), _reset_fingerprint(user["id"])):
        return None
    return user


def reset_link(token: str) -> str | None:
    base = settings.resolved_app_url
    return f"{base}/reset-password?token={token}" if base else None
