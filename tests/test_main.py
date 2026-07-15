from __future__ import annotations

"""Tests for the CLI entry point wiring."""

from typing import Any

from tradingbot import __main__ as cli
from tradingbot.config import load_config
from tradingbot.models import Candle


class _FakeFeed:
    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        del symbol, timeframe, limit
        return []

    def latest_closed_candle(self, symbol: str, timeframe: str) -> Candle | None:
        del symbol, timeframe
        return None


def test_main_builds_strategy_from_config(monkeypatch) -> None:
    """Verify that main wires the strategy, feed, and runtime from the config."""
    cfg = load_config({
        "API_KEY": "key",
        "API_SECRET": "secret",
        "STRATEGY": "custom",
    })
    feed = _FakeFeed()
    captured: dict[str, Any] = {}

    class _FakeRuntime:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def run_once(self) -> None:
            return None

    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli, "require_credentials", lambda config: None)
    monkeypatch.setattr(cli, "_build_venue", lambda config: object())
    monkeypatch.setattr(cli.CcxtCandleFeed, "from_exchange", classmethod(
        lambda cls, *args, **kwargs: feed,
    ))
    monkeypatch.setattr(cli, "build_strategy", lambda name, context: captured.update(
        strategy_name=name,
        strategy_context=context,
    ) or object())
    monkeypatch.setattr(cli, "BotRuntime", _FakeRuntime)

    assert cli.main([]) == 0
    context = captured["strategy_context"]
    assert captured["strategy_name"] == "custom"
    assert context.symbol == cfg.symbol
    assert context.timeframe == cfg.timeframe
    assert context.quantity == cfg.order_qty
    assert context.data_feed is feed
