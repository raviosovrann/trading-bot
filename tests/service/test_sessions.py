"""Tests for the server-side session store."""

from __future__ import annotations

from pathlib import Path

from tradingbot.service.sessions import SessionStore, hash_session_id
from tradingbot.service.store import BotStore


def _store(tmp_path: Path) -> BotStore:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return BotStore(data_dir)


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class TestCreateAndResolve:
    def test_create_returns_raw_id_and_csrf_and_persists_only_hash(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        sessions = SessionStore(store)

        raw_id, csrf = sessions.create("user-1")

        assert raw_id and csrf
        # The raw id is never stored; only its hash is.
        record = store.get_session(hash_session_id(raw_id))
        assert record is not None
        assert record["user_id"] == "user-1"
        assert store.get_session(raw_id) is None

    def test_resolve_returns_live_session(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        sessions = SessionStore(store)
        raw_id, csrf = sessions.create("user-1")

        session = sessions.resolve(raw_id)

        assert session is not None
        assert session.user_id == "user-1"
        assert session.csrf_token == csrf

    def test_resolve_unknown_or_none_is_none(self, tmp_path: Path) -> None:
        sessions = SessionStore(_store(tmp_path))
        assert sessions.resolve(None) is None
        assert sessions.resolve("does-not-exist") is None


class TestExpiry:
    def test_idle_timeout_expires_and_deletes(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        clock = _Clock()
        sessions = SessionStore(store, idle_ttl=100, absolute_ttl=10_000, clock=clock)
        raw_id, _ = sessions.create("user-1")

        clock.now += 101
        assert sessions.resolve(raw_id) is None
        assert store.get_session(hash_session_id(raw_id)) is None

    def test_activity_refreshes_idle_window(self, tmp_path: Path) -> None:
        clock = _Clock()
        sessions = SessionStore(_store(tmp_path), idle_ttl=100, absolute_ttl=10_000, clock=clock)
        raw_id, _ = sessions.create("user-1")

        clock.now += 50
        assert sessions.resolve(raw_id) is not None  # refreshes last_seen
        clock.now += 60  # 60 < 100 since the refresh
        assert sessions.resolve(raw_id) is not None

    def test_absolute_lifetime_expires_despite_activity(self, tmp_path: Path) -> None:
        clock = _Clock()
        sessions = SessionStore(_store(tmp_path), idle_ttl=1_000, absolute_ttl=200, clock=clock)
        raw_id, _ = sessions.create("user-1")

        clock.now += 150
        assert sessions.resolve(raw_id) is not None
        clock.now += 60  # total age 210 > 200
        assert sessions.resolve(raw_id) is None


class TestRevocation:
    def test_revoke_removes_single_session(self, tmp_path: Path) -> None:
        sessions = SessionStore(_store(tmp_path))
        raw_id, _ = sessions.create("user-1")

        sessions.revoke(raw_id)

        assert sessions.resolve(raw_id) is None

    def test_revoke_user_removes_all_their_sessions(self, tmp_path: Path) -> None:
        sessions = SessionStore(_store(tmp_path))
        a, _ = sessions.create("user-1")
        b, _ = sessions.create("user-1")
        other, _ = sessions.create("user-2")

        removed = sessions.revoke_user("user-1")

        assert removed == 2
        assert sessions.resolve(a) is None
        assert sessions.resolve(b) is None
        assert sessions.resolve(other) is not None
