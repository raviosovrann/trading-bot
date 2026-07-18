"""Password hashing for internal operator login.

Uses stdlib PBKDF2-HMAC-SHA256 (no third-party dependency), matching the
hashlib/hmac approach already used for bearer tokens. Hashes are stored in a
self-describing string ``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>`` so
the parameters travel with the hash and can be tuned without a schema change.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 240_000
_SALT_BYTES = 16
# Guard against corrupt records: a non-positive count makes pbkdf2_hmac raise,
# and an absurdly large one would hang the request. Both fail closed instead.
_MAX_ITERATIONS = 100_000_000

# Minimum operator password length. Internal-deployment policy; enforced by the
# admin CLI and any password-setting flow.
MIN_PASSWORD_LENGTH = 12


class WeakPasswordError(ValueError):
    """Raised when a proposed password does not meet the policy."""


def check_password_policy(password: str) -> None:
    """Validate ``password`` against the policy, raising on failure.

    Args:
        password: Proposed plaintext password.

    Raises:
        WeakPasswordError: If the password is too short or all whitespace.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    if not password.strip():
        raise WeakPasswordError("password must not be blank")


def needs_rehash(encoded: str) -> bool:
    """Return whether ``encoded`` should be re-hashed with current parameters.

    A stored hash produced with a different algorithm or a lower iteration count
    is upgraded transparently on the next successful login.

    Args:
        encoded: Encoded hash produced by :func:`hash_password`.

    Returns:
        ``True`` when the hash is stale (or unparseable) and should be replaced.
    """
    try:
        algo, iterations_str, _salt_hex, _hash_hex = encoded.split("$")
    except ValueError:
        return True
    if algo != _ALGO:
        return True
    try:
        iterations = int(iterations_str)
    except ValueError:
        return True
    return iterations < _ITERATIONS


def hash_password(password: str, *, iterations: int = _ITERATIONS) -> str:
    """Hash ``password`` with a fresh random salt.

    Args:
        password: Plaintext password to hash.
        iterations: PBKDF2 iteration count (higher is slower/stronger).

    Returns:
        Encoded string ``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>``.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGO}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Return whether ``password`` matches the ``encoded`` hash.

    Malformed or unknown-algorithm encodings return ``False`` rather than
    raising, so a corrupt user record simply fails authentication.

    Args:
        password: Plaintext password to check.
        encoded: Encoded hash produced by :func:`hash_password`.

    Returns:
        ``True`` if the password matches, ``False`` otherwise.
    """
    try:
        algo, iterations_str, salt_hex, expected_hex = encoded.split("$")
    except ValueError:
        return False
    if algo != _ALGO:
        return False
    try:
        iterations = int(iterations_str)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hex)
    except ValueError:
        return False
    if not 1 <= iterations <= _MAX_ITERATIONS:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)
