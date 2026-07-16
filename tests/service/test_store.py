"""Tests for the bot store persistence layer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def test_append_and_read_trades(tmp_path: Path) -> None:
    """Verify that trades can be appended and read back."""
    store = BotStore(tmp_path)
    store.append_trade("bot-1", {"action": "buy", "status": "filled"})
    store.append_trade("bot-1", {"action": "sell", "status": "filled"})

    trades = store.read_trades("bot-1")
    assert [t["action"] for t in trades] == ["buy", "sell"]


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
    (tmp_path / "secrets.json").write_text(json.dumps({"coinbase": {"spot": {"api_key": "x"}}}), encoding="utf-8")
    (tmp_path / "users.json").write_text(json.dumps({"users": [{"username": "u", "token_hash": "h"}]}), encoding="utf-8")
    store = BotStore(tmp_path)

    assert store.load_secrets()["coinbase"]["spot"]["api_key"] == "x"
    assert store.load_users()["users"][0]["username"] == "u"


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
