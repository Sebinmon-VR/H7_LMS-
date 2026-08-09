"""
Credential generation for admin-provisioned accounts.

Admins create a teacher or student by name only; this module derives the login email from
that name and mints the password. Nothing here touches Firebase or Firestore — callers own
persistence, which keeps the generators trivially testable and side-effect free.

Passwords are drawn from `secrets`, never `random`, and deliberately exclude visually
ambiguous characters (0/O, 1/l/I) because these credentials get read off a screen or an
email and typed by hand.
"""

import re
import secrets
import unicodedata
from typing import Callable

# Ambiguous glyphs removed: l, o, I, O, 0, 1.
_LOWER = "abcdefghijkmnpqrstuvwxyz"
_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_DIGITS = "23456789"
_SYMBOLS = "!@#$%*?"

_NON_SLUG = re.compile(r"[^a-z0-9]+")

# Firebase Auth rejects anything shorter than 6.
MIN_PASSWORD_LENGTH = 8
DEFAULT_PASSWORD_LENGTH = 12

# Guards the collision loop when a domain somehow has thousands of same-named users.
MAX_EMAIL_ATTEMPTS = 100


def _shuffled(items: list[str]) -> list[str]:
    """Fisher-Yates shuffle driven by `secrets`, so ordering leaks nothing."""
    result = list(items)
    for i in range(len(result) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        result[i], result[j] = result[j], result[i]
    return result


def generate_password(length: int = DEFAULT_PASSWORD_LENGTH) -> str:
    """
    Returns a random password containing at least one lowercase letter, uppercase letter,
    digit, and symbol, so it satisfies any password policy Firebase may later enforce.
    """
    length = max(length, MIN_PASSWORD_LENGTH)

    pools = [_LOWER, _UPPER, _DIGITS, _SYMBOLS]
    # One guaranteed character per class, then fill the remainder from everything.
    chars = [secrets.choice(pool) for pool in pools]
    everything = "".join(pools)
    chars += [secrets.choice(everything) for _ in range(length - len(chars))]

    return "".join(_shuffled(chars))


def slugify_name(full_name: str) -> str:
    """
    Converts a display name into the local part of an email address.

    Middle names are dropped ("Anita Rose Fernandes" -> "anita.fernandes") to match the
    firstname.lastname convention schools expect; the collision suffix handles the
    duplicates that convention inevitably produces. Accented and non-Latin characters are
    transliterated where possible, and a name that leaves nothing usable falls back to
    "user" so the caller always gets a valid address.
    """
    normalized = unicodedata.normalize("NFKD", full_name or "")
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()

    parts = [part for part in _NON_SLUG.split(ascii_only) if part]
    if not parts:
        return "user"
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]}.{parts[-1]}"


def generate_email(
    full_name: str,
    domain: str,
    is_taken: Callable[[str], bool] | None = None,
) -> str:
    """
    Builds a unique login email from a display name and domain.

    `is_taken` is called with each candidate address; the first one it rejects as free is
    returned. Collisions get a numeric suffix on the local part
    ("anita.fernandes2@school.com"), never on the domain.
    """
    domain = (domain or "").strip().lstrip("@").lower()
    if not domain:
        raise ValueError(
            "No email domain is configured. Set USER_EMAIL_DOMAIN (or "
            "GOOGLE_WORKSPACE_DOMAIN) before generating user emails."
        )

    local = slugify_name(full_name)
    candidate = f"{local}@{domain}"
    if is_taken is None:
        return candidate

    suffix = 1
    while is_taken(candidate) and suffix < MAX_EMAIL_ATTEMPTS:
        suffix += 1
        candidate = f"{local}{suffix}@{domain}"

    if is_taken(candidate):
        raise ValueError(
            f"Could not derive a free email address for '{full_name}' on '{domain}' "
            f"after {MAX_EMAIL_ATTEMPTS} attempts."
        )
    return candidate
