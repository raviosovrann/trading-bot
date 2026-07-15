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
    store = BotStore(tmp_path)
    store.save_config(_config("a"))
    store.save_config(_config("b"))

    text = (tmp_path / "bots.json").read_text(encoding="utf-8")
    data = json.loads(text)
    assert {c["id"] for c in data} == {"a", "b"}


def test_append_and_read_trades(tmp_path: Path) -> None:
    store = BotStore(tmp_path)
    store.append_trade("bot-1", {"action": "buy", "status": "filled"})
    store.append_trade("bot-1", {"action": "sell", "status": "filled"})

    trades = store.read_trades("bot-1")
    assert [t["action"] for t in trades] == ["buy", "sell"]


def test_read_trades_for_missing_bot_returns_empty_list(tmp_path: Path) -> None:
    store = BotStore(tmp_path)
    assert store.read_trades("missing") == []


@pytest.mark.parametrize("bot_id", ["../escape", "bad/id", "", "bot.id"])
def test_trade_path_validation_rejects_bad_bot_ids(tmp_path: Path, bot_id: str) -> None:
    store = BotStore(tmp_path)
    with pytest.raises(ValueError):
        store.append_trade(bot_id, {"action": "buy"})
    with pytest.raises(ValueError):
        store.read_trades(bot_id)


def test_load_secrets_and_users(tmp_path: Path) -> None:
    (tmp_path / "secrets.json").write_text(json.dumps({"coinbase": {"spot": {"api_key": "x"}}}), encoding="utf-8")
    (tmp_path / "users.json").write_text(json.dumps({"users": [{"username": "u", "token_hash": "h"}]}), encoding="utf-8")
    store = BotStore(tmp_path)

    assert store.load_secrets()["coinbase"]["spot"]["api_key"] == "x"
    assert store.load_users()["users"][0]["username"] == "u"
