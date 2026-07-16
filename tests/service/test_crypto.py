"""Tests for at-rest secret encryption helpers."""

from __future__ import annotations

import pytest

from tradingbot.service.crypto import decrypt, encrypt, generate_key


def test_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ciphertext decrypts back to the original plaintext."""
    monkeypatch.setenv("TRADINGBOT_SECRETS_KEY", generate_key())
    token = encrypt("super-secret-api-key")
    assert token != "super-secret-api-key"
    assert decrypt(token) == "super-secret-api-key"


def test_encrypt_without_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Encrypting without a configured key fails loudly rather than storing plaintext."""
    monkeypatch.delenv("TRADINGBOT_SECRETS_KEY", raising=False)
    with pytest.raises(RuntimeError):
        encrypt("x")


def test_decrypt_with_wrong_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token encrypted under one key does not decrypt under another."""
    monkeypatch.setenv("TRADINGBOT_SECRETS_KEY", generate_key())
    token = encrypt("x")
    monkeypatch.setenv("TRADINGBOT_SECRETS_KEY", generate_key())
    with pytest.raises(Exception):
        decrypt(token)
