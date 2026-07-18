"""Tests for the audit log: redaction, hash chain, and pagination."""

from __future__ import annotations

from pathlib import Path

from tradingbot.service.audit import AuditLog, redact
from tradingbot.service.principal import Principal
from tradingbot.service.store import BotStore


def _store(tmp_path: Path) -> BotStore:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return BotStore(data_dir)


def _principal() -> Principal:
    return Principal(id="u1", username="alice", roles=("admin",), kind="user")


class TestRedaction:
    def test_masks_sensitive_keys_at_any_depth(self) -> None:
        value = {
            "api_key": "AKIA",
            "nested": {"api_secret": "shh", "symbol": "BTC/USD"},
            "list": [{"password": "p"}],
            "keep": "visible",
        }
        out = redact(value)
        assert out["api_key"] == "***"
        assert out["nested"]["api_secret"] == "***"
        assert out["nested"]["symbol"] == "BTC/USD"
        assert out["list"][0]["password"] == "***"
        assert out["keep"] == "visible"

    def test_masks_keys_that_merely_contain_a_sensitive_token(self) -> None:
        assert redact({"api_key_id": "x"})["api_key_id"] == "***"

    def test_none_stays_none(self) -> None:
        assert redact(None) is None


class TestAuditLog:
    def test_record_persists_survives_restart(self, tmp_path: Path) -> None:
        log = AuditLog(_store(tmp_path))
        log.record(
            actor=_principal(), action="login", target="user:alice",
            request_id="r1", outcome="success",
        )
        # A fresh store instance reads the same records from disk.
        reopened = BotStore(tmp_path / "data")
        events, _ = reopened.read_audit()
        assert len(events) == 1
        assert events[0]["actor_name"] == "alice"
        assert events[0]["action"] == "login"

    def test_record_redacts_before_persisting(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        AuditLog(store).record(
            actor=_principal(), action="credentials.update", target="venue:coinbase/spot",
            request_id="r1", outcome="success", after={"api_secret": "leak-me"},
        )
        events, _ = store.read_audit()
        assert events[0]["after"]["api_secret"] == "***"
        # The plaintext secret is nowhere in the raw file.
        assert "leak-me" not in (tmp_path / "data" / "audit.jsonl").read_text()

    def test_chain_verifies_and_detects_tampering(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        log = AuditLog(store)
        for i in range(3):
            log.record(actor=_principal(), action="bot.start", target=f"bot:{i}",
                       request_id="r", outcome="success")
        assert store.verify_audit_chain() is True

        # Tamper with the middle record's action.
        audit_file = tmp_path / "data" / "audit.jsonl"
        lines = audit_file.read_text().splitlines()
        lines[1] = lines[1].replace("bot.start", "bot.stop")
        audit_file.write_text("\n".join(lines) + "\n")
        assert store.verify_audit_chain() is False

    def test_pagination_returns_newest_first_with_cursor(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        log = AuditLog(store)
        for i in range(5):
            log.record(actor=_principal(), action="bot.start", target=f"bot:{i}",
                       request_id="r", outcome="success")
        page1, cursor = store.read_audit(limit=2)
        assert [e["seq"] for e in page1] == [5, 4]
        assert cursor == 4
        page2, cursor2 = store.read_audit(limit=2, before_seq=cursor)
        assert [e["seq"] for e in page2] == [3, 2]
        assert cursor2 == 2

    def test_audit_file_is_owner_only(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        AuditLog(store).record(actor=_principal(), action="login", target="user:alice",
                               request_id="r", outcome="success")
        mode = (tmp_path / "data" / "audit.jsonl").stat().st_mode & 0o777
        assert mode == 0o600
