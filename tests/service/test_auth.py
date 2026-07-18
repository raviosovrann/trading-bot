"""Tests for password hashing/verification helpers."""

from __future__ import annotations

import pytest

from tradingbot.service.auth import (
    MIN_PASSWORD_LENGTH,
    WeakPasswordError,
    check_password_policy,
    hash_password,
    needs_rehash,
    verify_password,
)


def test_policy_accepts_a_strong_password() -> None:
    check_password_policy("a" * MIN_PASSWORD_LENGTH)


def test_policy_rejects_short_password() -> None:
    with pytest.raises(WeakPasswordError):
        check_password_policy("a" * (MIN_PASSWORD_LENGTH - 1))


def test_policy_rejects_blank_password() -> None:
    with pytest.raises(WeakPasswordError):
        check_password_policy(" " * (MIN_PASSWORD_LENGTH + 2))


def test_needs_rehash_false_for_current_params() -> None:
    assert needs_rehash(hash_password("some-password")) is False


def test_needs_rehash_true_for_low_iterations() -> None:
    assert needs_rehash(hash_password("some-password", iterations=1000)) is True


def test_needs_rehash_true_for_garbage() -> None:
    assert needs_rehash("not-a-valid-hash") is True


def test_hash_is_not_plaintext() -> None:
    """The encoded hash must not contain the raw password."""
    encoded = hash_password("hunter2")
    assert "hunter2" not in encoded


def test_verify_accepts_correct_password() -> None:
    """A password verifies against its own hash."""
    encoded = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", encoded) is True


def test_verify_rejects_wrong_password() -> None:
    """A different password does not verify."""
    encoded = hash_password("correct horse battery staple")
    assert verify_password("Tr0ubador&3", encoded) is False


def test_same_password_hashes_differently() -> None:
    """A random per-hash salt makes two hashes of the same password differ."""
    assert hash_password("same") != hash_password("same")


def test_verify_rejects_malformed_encoding() -> None:
    """Garbage or empty encodings verify as False rather than raising."""
    assert verify_password("whatever", "") is False
    assert verify_password("whatever", "not$a$valid$hash") is False


def test_verify_rejects_nonpositive_iterations() -> None:
    """Zero/negative iteration counts fail closed instead of raising ValueError."""
    assert verify_password("whatever", "pbkdf2_sha256$0$aa$bb") is False
    assert verify_password("whatever", "pbkdf2_sha256$-5$aa$bb") is False
