"""Deleting a bot: supervisor, store and history handling (#163)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tradingbot.models import Candle, Order, OrderResult, Position
from tradingbot.service.events import EventBus
from tradingbot.service.risk import GlobalExposure
from tradingbot.service.store import BotStore
from tradingbot.service.supervisor import BotConfig, BotSupervisor


def _candle(ts: int = 1, close: float = 100.0) -> Candle:
    return Candle(timestamp=ts, open=close, high=close, low=close, close=close, volume=1.0)


class _FakeHub:
    async def warmup(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        del symbol, timeframe, limit
        return [_candle()]

    def subscribe(self, symbol, timeframe, handler) -> None: ...
    def unsubscribe(self, symbol, timeframe, handler) -> None: ...

    def latest_price(self, symbol, timeframe) -> float | None:
        return 100.0


class _FakeVenue:
    def place_order(self, order: Order) -> OrderResult:
        return OrderResult(ok=True, order_id="o1", status="filled", filled_qty=order.qty, raw={})

    def close_position(self, symbol: str) -> OrderResult:
        return OrderResult(ok=True, order_id=None, status="none", filled_qty=0.0, raw={})

    def get_position(self, symbol: str) -> Position | None:
        return None

    def health_check(self) -> bool:
        return True


class _IdleStrategy:
    def on_bar(self, candles):
        return None


def _config(bot_id: str) -> BotConfig:
    return BotConfig(
        id=bot_id, venue="coinbase", market_type="spot", strategy="example",
        symbol="BTC/USD", timeframe="1m", quantity=0.1, live=False,
        per_bot_cap=1000.0, global_cap=10_000.0, params={},
    )


def _supervisor(monkeypatch, store=None) -> BotSupervisor:
    monkeypatch.setattr("tradingbot.service.supervisor.build_venue", lambda *a, **k: _FakeVenue())
    monkeypatch.setattr("tradingbot.service.supervisor.build_strategy", lambda *a, **k: _IdleStrategy())
    return BotSupervisor(
        hub_factory=lambda cfg: _FakeHub(),
        event_bus=EventBus(),
        global_exposure=GlobalExposure(),
        store=store,
    )


class TestSupervisorRemove:
    @pytest.mark.asyncio
    async def test_a_stopped_bot_can_be_removed(self, monkeypatch) -> None:
        """Verify a stopped bot is dropped from the supervisor."""
        supervisor = _supervisor(monkeypatch)
        supervisor.create(_config("bot-1"))

        await supervisor.remove("bot-1")

        assert supervisor.get("bot-1") is None
        assert supervisor.list() == []

    @pytest.mark.asyncio
    async def test_removing_a_running_bot_is_refused(self, monkeypatch) -> None:
        """Verify a running bot cannot be deleted out from under itself.

        It may hold an open position; deleting it would strand that position
        with nothing left to manage it.
        """
        supervisor = _supervisor(monkeypatch)
        supervisor.create(_config("bot-1"))
        await supervisor.start("bot-1")

        with pytest.raises(ValueError, match="running"):
            await supervisor.remove("bot-1")

        assert supervisor.get("bot-1") is not None
        await supervisor.stop("bot-1")

    @pytest.mark.asyncio
    async def test_removing_an_unknown_bot_raises(self, monkeypatch) -> None:
        """Verify an unknown id is a clear error, not a silent success."""
        supervisor = _supervisor(monkeypatch)
        with pytest.raises(KeyError):
            await supervisor.remove("nope")

    @pytest.mark.asyncio
    async def test_a_concurrent_delete_and_start_cannot_interleave(self, monkeypatch) -> None:
        """Verify delete and start serialize on the bot's lifecycle lock (#126).

        Without the lock a delete could land between a start's checks and its
        task creation, leaving a running runtime with no bot to own it.
        """
        supervisor = _supervisor(monkeypatch)
        supervisor.create(_config("bot-1"))

        results = await asyncio.gather(
            supervisor.start("bot-1"),
            supervisor.remove("bot-1"),
            return_exceptions=True,
        )

        removed = supervisor.get("bot-1") is None
        errors = [r for r in results if isinstance(r, BaseException)]
        if removed:
            # Delete won: the start must have been refused or undone cleanly.
            assert supervisor.list() == []
        else:
            # Start won: the delete must have been refused, not partially applied.
            assert errors, "one of the two operations had to lose"
            await supervisor.stop("bot-1")

    @pytest.mark.asyncio
    async def test_removing_one_bot_leaves_the_others(self, monkeypatch) -> None:
        """Verify deletion is scoped to its own bot."""
        supervisor = _supervisor(monkeypatch)
        supervisor.create(_config("bot-1"))
        supervisor.create(_config("bot-2"))

        await supervisor.remove("bot-1")

        assert supervisor.get("bot-2") is not None
        assert [b.config.id for b in supervisor.list()] == ["bot-2"]


class TestStoreDelete:
    def test_delete_config_removes_only_that_bot(self, tmp_path: Path) -> None:
        """Verify the persisted record is dropped and the others survive."""
        store = BotStore(tmp_path)
        store.save_config(_config("bot-1"))
        store.save_config(_config("bot-2"))

        store.delete_config("bot-1")

        assert [c.id for c in store.load_configs()] == ["bot-2"]

    def test_deleting_an_absent_config_is_a_no_op(self, tmp_path: Path) -> None:
        """Verify a repeat delete does not raise."""
        store = BotStore(tmp_path)
        store.save_config(_config("bot-1"))

        store.delete_config("nope")

        assert [c.id for c in store.load_configs()] == ["bot-1"]

    def test_delete_rejects_an_unsafe_bot_id(self, tmp_path: Path) -> None:
        """Verify path traversal is refused here as everywhere else."""
        store = BotStore(tmp_path)
        with pytest.raises(ValueError):
            store.delete_config("../escape")

    def test_trade_history_is_archived_not_destroyed(self, tmp_path: Path) -> None:
        """Verify deleting a bot preserves its trades.

        The retention policy set in #122 is rotate-never-delete; a bot going
        away must not be a back door that destroys executed-trade records.
        """
        store = BotStore(tmp_path, trade_rotate_bytes=400)
        for n in range(30):
            store.append_trade("bot-1", {"bot_id": "bot-1", "action": "buy", "order_id": f"o{n}"})
        live_segments = sorted(p.name for p in (tmp_path / "trades").glob("bot-1*.jsonl"))
        assert len(live_segments) > 1, "test needs multiple segments to be meaningful"

        store.archive_trades("bot-1")

        assert list((tmp_path / "trades").glob("bot-1*.jsonl")) == []
        archived = sorted(p.name for p in (tmp_path / "trades" / "archive" / "bot-1").glob("*.jsonl"))
        assert archived == live_segments, "every segment must survive, not just the active one"

    def test_archiving_preserves_every_record(self, tmp_path: Path) -> None:
        """Verify the archived content is byte-for-byte the trades written."""
        store = BotStore(tmp_path, trade_rotate_bytes=400)
        for n in range(25):
            store.append_trade("bot-1", {"bot_id": "bot-1", "action": "buy", "order_id": f"o{n}"})

        store.archive_trades("bot-1")

        archive_dir = tmp_path / "trades" / "archive" / "bot-1"
        rows = []
        for path in sorted(archive_dir.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        assert [r["order_id"] for r in rows] == [f"o{n}" for n in range(25)]

    def test_archiving_a_bot_without_history_is_a_no_op(self, tmp_path: Path) -> None:
        """Verify a bot that never traded deletes cleanly."""
        store = BotStore(tmp_path)
        store.archive_trades("bot-1")
        assert not (tmp_path / "trades" / "archive" / "bot-1").exists()

    def test_archiving_leaves_other_bots_history_alone(self, tmp_path: Path) -> None:
        """Verify one bot's deletion cannot take another's trades with it."""
        store = BotStore(tmp_path)
        store.append_trade("bot-1", {"bot_id": "bot-1", "action": "buy"})
        store.append_trade("bot-2", {"bot_id": "bot-2", "action": "sell"})

        store.archive_trades("bot-1")

        remaining, _ = store.read_trades("bot-2")
        assert len(remaining) == 1
        assert remaining[0]["action"] == "sell"
