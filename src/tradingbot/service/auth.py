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
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)
