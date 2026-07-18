"""Tests for the tradingbot admin CLI (bootstrap + user management)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tradingbot import admin
from tradingbot.service.auth import verify_password
from tradingbot.service.sessions import SessionStore
from tradingbot.service.store import BotStore

_STRONG = "correct-horse-battery-staple"


def _run(tmp_path: Path, *argv: str, password: str = _STRONG) -> int:
    return admin.main(list(argv), read_password=lambda: password, data_dir=str(tmp_path / "data"))


def _users(tmp_path: Path) -> list[dict]:
    return BotStore(tmp_path / "data").load_users().get("users", [])


class TestBootstrap:
    def test_creates_first_admin(self, tmp_path: Path) -> None:
        assert _run(tmp_path, "bootstrap", "--username", "admin") == 0
        users = _users(tmp_path)
        assert len(users) == 1
        assert users[0]["username"] == "admin"
        assert users[0]["roles"] == ["admin"]
        assert verify_password(_STRONG, users[0]["password_hash"])

    def test_refuses_when_users_exist(self, tmp_path: Path) -> None:
        _run(tmp_path, "bootstrap", "--username", "admin")
        assert _run(tmp_path, "bootstrap", "--username", "other") == 2
        assert len(_users(tmp_path)) == 1

    def test_rejects_weak_password(self, tmp_path: Path) -> None:
        assert _run(tmp_path, "bootstrap", "--username", "admin", password="short") == 2
        assert _users(tmp_path) == []


class TestUserManagement:
    def test_add_operator_and_admin(self, tmp_path: Path) -> None:
        _run(tmp_path, "bootstrap", "--username", "admin")
        assert _run(tmp_path, "user", "add", "--username", "op") == 0
        assert _run(tmp_path, "user", "add", "--username", "boss", "--admin") == 0
        by_name = {u["username"]: u for u in _users(tmp_path)}
        assert by_name["op"]["roles"] == ["operator"]
        assert by_name["boss"]["roles"] == ["admin"]

    def test_duplicate_add_fails(self, tmp_path: Path) -> None:
        _run(tmp_path, "bootstrap", "--username", "admin")
        assert _run(tmp_path, "user", "add", "--username", "op") == 0
        assert _run(tmp_path, "user", "add", "--username", "op") == 2

    def test_disable_unknown_user_errors(self, tmp_path: Path) -> None:
        _run(tmp_path, "bootstrap", "--username", "admin")
        assert _run(tmp_path, "user", "disable", "--username", "ghost") == 2

    def test_disable_revokes_sessions(self, tmp_path: Path) -> None:
        _run(tmp_path, "bootstrap", "--username", "admin")
        store = BotStore(tmp_path / "data")
        user_id = _users(tmp_path)[0]["id"]
        raw_id, _ = SessionStore(store).create(user_id)

        assert _run(tmp_path, "user", "disable", "--username", "admin") == 0

        assert BotStore(tmp_path / "data").load_users()["users"][0]["disabled"] is True
        assert SessionStore(BotStore(tmp_path / "data")).resolve(raw_id) is None

    def test_reset_password_changes_hash_and_revokes(self, tmp_path: Path) -> None:
        _run(tmp_path, "bootstrap", "--username", "admin")
        store = BotStore(tmp_path / "data")
        raw_id, _ = SessionStore(store).create(_users(tmp_path)[0]["id"])

        assert _run(tmp_path, "user", "reset-password", "--username", "admin", password="a-new-strong-pass") == 0

        user = _users(tmp_path)[0]
        assert verify_password("a-new-strong-pass", user["password_hash"])
        assert SessionStore(BotStore(tmp_path / "data")).resolve(raw_id) is None

    def test_revoke_sessions(self, tmp_path: Path) -> None:
        _run(tmp_path, "bootstrap", "--username", "admin")
        store = BotStore(tmp_path / "data")
        raw_id, _ = SessionStore(store).create(_users(tmp_path)[0]["id"])
        assert _run(tmp_path, "user", "revoke-sessions", "--username", "admin") == 0
        assert SessionStore(BotStore(tmp_path / "data")).resolve(raw_id) is None

    def test_list_shows_users(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _run(tmp_path, "bootstrap", "--username", "admin")
        _run(tmp_path, "user", "add", "--username", "op")
        capsys.readouterr()  # drop prior output
        _run(tmp_path, "user", "list")
        out = capsys.readouterr().out
        assert "admin" in out and "op" in out


class TestSecurityProperties:
    def test_password_is_not_a_cli_argument(self) -> None:
        """No subcommand accepts a --password flag (it would leak into history)."""
        parser = admin._build_parser()
        # argparse exits with code 2 on an unknown option.
        with pytest.raises(SystemExit):
            parser.parse_args(["user", "add", "--username", "op", "--password", "x"])

    def test_users_file_is_owner_only(self, tmp_path: Path) -> None:
        _run(tmp_path, "bootstrap", "--username", "admin")
        data_dir = tmp_path / "data"
        assert (data_dir.stat().st_mode & 0o777) == 0o700
        assert ((data_dir / "users.json").stat().st_mode & 0o777) == 0o600
