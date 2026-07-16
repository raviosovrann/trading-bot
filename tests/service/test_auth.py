"""Tests for password hashing/verification helpers."""

from __future__ import annotations

from tradingbot.service.auth import hash_password, verify_password


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
