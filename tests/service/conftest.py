"""Shared fixtures for service tests."""

from __future__ import annotations

import pytest

from tradingbot.service.crypto import generate_key

# One key for the whole test session so encrypt/decrypt round-trips consistently.
_TEST_SECRETS_KEY = generate_key()


@pytest.fixture(autouse=True)
def _secrets_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a secrets-encryption key for every service test."""
    monkeypatch.setenv("TRADINGBOT_SECRETS_KEY", _TEST_SECRETS_KEY)
