"""Admin password hashing (argon2id)."""

from __future__ import annotations

import hmac
import re

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 12

_hasher = PasswordHasher()  # argon2id with library-recommended parameters


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    if not password_hash:
        # Still spend comparable time so a missing account is not observable.
        _hasher.hash(password)
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


USERNAME_MAX = 64
_USERNAME_RE = re.compile(r"^[\w.@+-]{1,64}$")


def normalize_username(value: str) -> str | None:
    """Validated username (letters incl. CJK, digits, . _ @ + -), or None."""
    v = (value or "").strip()
    return v if _USERNAME_RE.fullmatch(v) else None


def usernames_match(stored: str | None, supplied: str) -> bool:
    """Case-insensitive, constant-time comparison."""
    a = (stored or "").casefold().encode()
    b = (supplied or "").strip().casefold().encode()
    return bool(stored) and hmac.compare_digest(a, b)
