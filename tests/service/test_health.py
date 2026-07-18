"""Tests for readiness probes and startup validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from tradingbot.service.health import (
    StartupError,
    check_secrets_key,
    check_storage,
    readiness,
    validate_startup,
)
from tradingbot.service.store import BotStore


def _store(tmp_path: Path) -> BotStore:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return BotStore(data_dir)


class TestStorageCheck:
    def test_writable_directory_is_ok(self, tmp_path: Path) -> None:
        assert check_storage(_store(tmp_path)).ok is True

    def test_read_only_directory_is_not_ok(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.data_dir.chmod(0o500)  # read+execute only
        try:
            result = check_storage(store)
        finally:
            store.data_dir.chmod(0o700)
        assert result.ok is False
        assert "not writable" in result.detail


class TestSecretsKeyCheck:
    def test_missing_key_is_not_ok(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TRADINGBOT_SECRETS_KEY", raising=False)
        result = check_secrets_key(_store(tmp_path))
        assert result.ok is False
        assert "not set" in result.detail

    def test_key_with_no_secrets_yet_is_ok(self, tmp_path: Path) -> None:
        # The autouse fixture provides a valid key; no secrets.json written yet.
        assert check_secrets_key(_store(tmp_path)).ok is True

    def test_key_that_decrypts_stored_secrets_is_ok(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.save_secrets("coinbase", "spot", {"api_key": "k"})
        assert check_secrets_key(store).ok is True

    def test_wrong_key_cannot_decrypt(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from tradingbot.service.crypto import generate_key

        store = _store(tmp_path)
        store.save_secrets("coinbase", "spot", {"api_key": "k"})
        # Rotate to a different key: continuity is broken.
        monkeypatch.setenv("TRADINGBOT_SECRETS_KEY", generate_key())
        result = check_secrets_key(store)
        assert result.ok is False
        assert "cannot decrypt" in result.detail


class TestReadiness:
    def test_healthy_store_is_ready(self, tmp_path: Path) -> None:
        ready, checks = readiness(_store(tmp_path))
        assert ready is True
        assert set(checks) == {"storage", "secrets_key"}

    def test_missing_key_makes_it_not_ready(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TRADINGBOT_SECRETS_KEY", raising=False)
        ready, checks = readiness(_store(tmp_path))
        assert ready is False
        assert checks["secrets_key"]["ok"] is False


class TestStartupValidation:
    def test_healthy_non_production_reports_no_problems(self, tmp_path: Path) -> None:
        assert validate_startup(_store(tmp_path)) == []

    def test_non_production_reports_but_does_not_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TRADINGBOT_SECRETS_KEY", raising=False)
        problems = validate_startup(_store(tmp_path))
        assert any("secrets_key" in p for p in problems)

    def test_production_fails_closed_on_broken_dependency(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRADINGBOT_ENV", "production")
        monkeypatch.setenv("TRADINGBOT_ALLOWED_ORIGINS", "https://console.example")
        monkeypatch.delenv("TRADINGBOT_SECRETS_KEY", raising=False)
        with pytest.raises(StartupError):
            validate_startup(_store(tmp_path))

    def test_production_requires_allowed_origins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRADINGBOT_ENV", "production")
        monkeypatch.delenv("TRADINGBOT_ALLOWED_ORIGINS", raising=False)
        with pytest.raises(StartupError, match="ALLOWED_ORIGINS"):
            validate_startup(_store(tmp_path))

    def test_production_rejects_disabled_secure_cookies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRADINGBOT_ENV", "production")
        monkeypatch.setenv("TRADINGBOT_ALLOWED_ORIGINS", "https://console.example")
        monkeypatch.setenv("TRADINGBOT_COOKIE_SECURE", "false")
        with pytest.raises(StartupError, match="COOKIE_SECURE"):
            validate_startup(_store(tmp_path))

    def test_healthy_production_config_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRADINGBOT_ENV", "production")
        monkeypatch.setenv("TRADINGBOT_ALLOWED_ORIGINS", "https://console.example")
        assert validate_startup(_store(tmp_path)) == []
