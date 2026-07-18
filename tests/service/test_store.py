"""Tests for the bot store persistence layer."""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
from pathlib import Path
from typing import Any

import pytest

import tradingbot.service.store as store_module
from tradingbot.service.store import BotStore
from tradingbot.service.supervisor import BotConfig


def _config(bot_id: str = "bot-1") -> BotConfig:
    return BotConfig(
        id=bot_id,
        venue="coinbase",
        market_type="spot",
        strategy="example",
        symbol="BTC/USD",
        timeframe="1m",
        quantity=0.1,
        live=False,
        per_bot_cap=1_000.0,
        global_cap=10_000.0,
        params={},
        creds={"api_key": "secret"},
    )


def _save_config_process(data_dir: str, bot_id: str, barrier: Any) -> None:
    """Save one config after all worker processes are ready."""
    store = BotStore(data_dir)
    barrier.wait()
    store.save_config(_config(bot_id))


def _save_secret_process(data_dir: str, venue: str, barrier: Any) -> None:
    """Save one venue secret after all worker processes are ready."""
    store = BotStore(data_dir)
    barrier.wait()
    store.save_secrets(venue, "spot", {"api_key": f"key-{venue}"})


def _update_user_process(data_dir: str, username: str, barrier: Any) -> None:
    """Update one user after all worker processes are ready."""
    store = BotStore(data_dir)
    barrier.wait()
    updated = store.update_user(username, updates={"token_hash": f"token-{username}"})
    if not updated:
        raise RuntimeError(f"user disappeared during update: {username}")


def _run_processes(target: Any, data_dir: Path, values: list[str]) -> None:
    """Run synchronized store writers and require clean worker exits."""
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(len(values))
    processes = [
        context.Process(target=target, args=(str(data_dir), value, barrier))
        for value in values
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert not process.is_alive(), "store writer did not finish"
        assert process.exitcode == 0


def test_save_and_load_config_excludes_creds(tmp_path: Path) -> None:
    """Verify that save/load config excludes credentials from stored data."""
    store = BotStore(tmp_path)
    cfg = _config()
    store.save_config(cfg)

    loaded = store.load_configs()
    assert len(loaded) == 1
    assert loaded[0].id == cfg.id
    assert loaded[0].creds == {}

    raw = json.loads((tmp_path / "bots.json").read_text(encoding="utf-8"))
    assert "creds" not in raw[0]
    assert "api_key" not in json.dumps(raw)


def test_save_config_is_atomic(tmp_path: Path) -> None:
    """Verify that saving multiple configs atomically writes both bots."""
    store = BotStore(tmp_path)
    store.save_config(_config("a"))
    store.save_config(_config("b"))

    text = (tmp_path / "bots.json").read_text(encoding="utf-8")
    data = json.loads(text)
    assert {c["id"] for c in data} == {"a", "b"}


def test_concurrent_processes_do_not_lose_configs(tmp_path: Path) -> None:
    """Independent processes preserve every unrelated bot config update."""
    bot_ids = [f"bot-{index}" for index in range(6)]
    _run_processes(_save_config_process, tmp_path, bot_ids)
    assert {config.id for config in BotStore(tmp_path).load_configs()} == set(bot_ids)


def test_append_and_read_trades(tmp_path: Path) -> None:
    """Verify that trades can be appended and read back."""
    store = BotStore(tmp_path)
    store.append_trade("bot-1", {"action": "buy", "status": "filled"})
    store.append_trade("bot-1", {"action": "sell", "status": "filled"})

    trades = store.read_trades("bot-1")
    assert [t["action"] for t in trades] == ["buy", "sell"]


def test_store_paths_are_owner_only_even_with_permissive_umask(tmp_path: Path) -> None:
    """Sensitive directories and files do not inherit permissive process modes."""
    data_dir = tmp_path / "data"
    previous_umask = os.umask(0)
    try:
        store = BotStore(data_dir)
        store.save_config(_config())
        store.save_users({"users": []})
        store.save_secrets("coinbase", "spot", {"api_key": "secret"})
        store.append_trade("bot-1", {"status": "filled"})
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((data_dir / "trades").stat().st_mode) == 0o700
    for path in [
        data_dir / ".store.lock",
        data_dir / "bots.json",
        data_dir / "users.json",
        data_dir / "secrets.json",
        data_dir / "trades" / "bot-1.jsonl",
    ]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_store_rejects_platforms_without_process_locking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unsupported platforms fail explicitly instead of allowing unsafe writers."""
    monkeypatch.setattr(store_module, "_fcntl", None)
    with pytest.raises(RuntimeError, match="POSIX flock"):
        BotStore(tmp_path)


def test_read_trades_for_missing_bot_returns_empty_list(tmp_path: Path) -> None:
    """Verify that reading trades for a missing bot returns an empty list."""
    store = BotStore(tmp_path)
    assert store.read_trades("missing") == []


@pytest.mark.parametrize("bot_id", ["../escape", "bad/id", "", "bot.id"])
def test_trade_path_validation_rejects_bad_bot_ids(tmp_path: Path, bot_id: str) -> None:
    """Verify that trade path validation rejects invalid bot IDs."""
    store = BotStore(tmp_path)
    with pytest.raises(ValueError):
        store.append_trade(bot_id, {"action": "buy"})
    with pytest.raises(ValueError):
        store.read_trades(bot_id)


def test_load_secrets_and_users(tmp_path: Path) -> None:
    """Verify that secrets and users are loaded from their respective files."""
    (tmp_path / "users.json").write_text(json.dumps({"users": [{"username": "u", "token_hash": "h"}]}), encoding="utf-8")
    store = BotStore(tmp_path)
    store.save_secrets("coinbase", "spot", {"api_key": "x"})

    assert store.load_secrets()["coinbase"]["spot"]["api_key"] == "x"
    assert store.load_users()["users"][0]["username"] == "u"


def test_secrets_are_encrypted_on_disk(tmp_path: Path) -> None:
    """The secrets file must not contain credential values in clear text."""
    store = BotStore(tmp_path)
    store.save_secrets("coinbase", "spot", {"api_key": "plaintext-should-not-appear"})
    raw = (tmp_path / "secrets.json").read_text(encoding="utf-8")
    assert "plaintext-should-not-appear" not in raw
    assert "api_key" not in raw


def test_save_users_round_trips(tmp_path: Path) -> None:
    """Users saved via save_users are read back by load_users."""
    store = BotStore(tmp_path)
    data = {"users": [{"username": "alice", "token_hash": "h", "password_hash": "p"}]}
    store.save_users(data)
    assert store.load_users() == data


def test_save_users_is_atomic(tmp_path: Path) -> None:
    """save_users leaves no temp files behind after writing."""
    store = BotStore(tmp_path)
    store.save_users({"users": []})
    assert not list(tmp_path.glob("*.tmp"))


def test_interrupted_atomic_write_preserves_original_and_removes_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed replace leaves the last durable value and no abandoned temp file."""
    store = BotStore(tmp_path)
    original = {"users": [{"username": "alice"}]}
    store.save_users(original)

    def fail_replace(source: str, destination: str) -> None:
        del source, destination
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        store.save_users({"users": [{"username": "bob"}]})

    assert store.load_users() == original
    assert not list(tmp_path.glob("*.tmp"))


def test_save_secrets_stores_creds_for_venue_market(tmp_path: Path) -> None:
    """save_secrets persists creds under [venue][market_type] for later load."""
    store = BotStore(tmp_path)
    store.save_secrets("coinbase", "spot", {"api_key": "k", "api_secret": "s"})
    assert store.load_secrets()["coinbase"]["spot"] == {"api_key": "k", "api_secret": "s"}


def test_save_secrets_merges_without_clobbering_other_entries(tmp_path: Path) -> None:
    """Saving one venue/market pair leaves other stored secrets intact."""
    store = BotStore(tmp_path)
    store.save_secrets("coinbase", "spot", {"api_key": "k1"})
    store.save_secrets("tradovate", "futures", {"name": "u"})
    store.save_secrets("coinbase", "spot", {"api_key": "k2"})  # overwrite same pair
    secrets = store.load_secrets()
    assert secrets["coinbase"]["spot"] == {"api_key": "k2"}
    assert secrets["tradovate"]["futures"] == {"name": "u"}


def test_concurrent_processes_do_not_lose_secrets(tmp_path: Path) -> None:
    """Independent processes preserve every unrelated credential update."""
    venues = [f"venue-{index}" for index in range(4)]
    _run_processes(_save_secret_process, tmp_path, venues)
    secrets = BotStore(tmp_path).load_secrets()
    assert set(secrets) == set(venues)


def test_concurrent_processes_do_not_lose_user_updates(tmp_path: Path) -> None:
    """Atomic field updates preserve sessions rotated for different users."""
    usernames = [f"user-{index}" for index in range(4)]
    store = BotStore(tmp_path)
    store.save_users({
        "users": [
            {"username": username, "password_hash": "password", "token_hash": "old"}
            for username in usernames
        ]
    })

    _run_processes(_update_user_process, tmp_path, usernames)

    users = {user["username"]: user for user in store.load_users()["users"]}
    assert {username: users[username]["token_hash"] for username in usernames} == {
        username: f"token-{username}" for username in usernames
    }


def test_update_user_can_compare_expected_fields(tmp_path: Path) -> None:
    """A stale login cannot overwrite a concurrently changed user record."""
    store = BotStore(tmp_path)
    store.save_users({
        "users": [{"username": "alice", "password_hash": "current", "token_hash": "old"}]
    })

    assert not store.update_user(
        "alice",
        expected={"password_hash": "stale"},
        updates={"token_hash": "unsafe"},
    )
    assert store.update_user(
        "alice",
        expected={"password_hash": "current"},
        updates={"token_hash": "new"},
    )
    assert store.load_users()["users"][0]["token_hash"] == "new"


def test_load_configs_handles_missing_empty_and_invalid_files(tmp_path: Path) -> None:
    """Verify load_configs tolerates missing, empty and malformed files."""
    store = BotStore(tmp_path)
    assert store.load_configs() == []

    (tmp_path / "bots.json").write_text("   ", encoding="utf-8")
    assert store.load_configs() == []

    (tmp_path / "bots.json").write_text("not-json", encoding="utf-8")
    assert store.load_configs() == []

    (tmp_path / "bots.json").write_text(json.dumps({"not": "list"}), encoding="utf-8")
    assert store.load_configs() == []


def test_read_trades_skips_empty_and_invalid_lines(tmp_path: Path) -> None:
    """Verify read_trades ignores blank lines and invalid JSON."""
    store = BotStore(tmp_path)
    store.append_trade("bot-1", {"action": "buy"})
    path = tmp_path / "trades" / "bot-1.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write("\n\nnot-json\n")
    store.append_trade("bot-1", {"action": "sell"})

    trades = store.read_trades("bot-1")
    assert [t["action"] for t in trades] == ["buy", "sell"]


def test_load_json_returns_empty_on_missing_or_invalid(tmp_path: Path) -> None:
    """Verify load_secrets/load_users return empty dicts for missing or bad files."""
    store = BotStore(tmp_path)
    assert store.load_secrets() == {}
    assert store.load_users() == {}

    (tmp_path / "secrets.json").write_text("garbage", encoding="utf-8")
    assert store.load_secrets() == {}

    (tmp_path / "users.json").write_text("null", encoding="utf-8")
    assert store.load_users() == {}
