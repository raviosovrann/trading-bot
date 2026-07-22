"""Order-status polling and reconciliation (#135, commits 3 and 4).

A venue that acknowledges an order before filling it -- Tradovate always, ccxt
often -- leaves the ledger holding a `submitted` order that will never advance
on its own. The supervisor has to go back and ask. These tests pin what it asks
about, what it does with the answer, and, mostly, what it refuses to conclude
when the answer does not arrive.
"""

from __future__ import annotations

import asyncio

import pytest

from tradingbot.models import Candle, Order, OrderResult, Position
from tradingbot.service.events import EventBus
from tradingbot.service.ledger import OrderState
from tradingbot.service.exposure import ExposureTracker
from tradingbot.service.supervisor import BotConfig, BotSupervisor


def _candle(ts: int = 1, close: float = 100.0) -> Candle:
    return Candle(timestamp=ts, open=close, high=close, low=close, close=close, volume=1.0)


class _Hub:
    def __init__(self) -> None:
        self.handlers: dict[tuple[str, str], list] = {}

    async def warmup(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        del symbol, timeframe, limit
        return [_candle()]

    def subscribe(self, symbol: str, timeframe: str, handler) -> None:
        self.handlers.setdefault((symbol, timeframe), []).append(handler)

    def unsubscribe(self, symbol: str, timeframe: str, handler) -> None:
        self.handlers[(symbol, timeframe)].remove(handler)

    def latest_price(self, symbol: str, timeframe: str) -> float | None:
        del symbol, timeframe
        return 100.0


class _AckOnlyVenue:
    """Acknowledges orders without filling them, then answers status polls."""

    def __init__(self, status: OrderResult | None = None) -> None:
        self.status = status
        self.fetched: list[str] = []

    def place_order(self, order: Order) -> OrderResult:
        return OrderResult(
            ok=True, order_id="v1", status="open", filled_qty=0.0, raw={}
        )

    def fetch_order(self, venue_order_id: str, symbol: str) -> OrderResult:
        del symbol
        self.fetched.append(venue_order_id)
        if self.status is None:
            raise RuntimeError("venue unreachable")
        return self.status

    def close_position(self, symbol: str) -> OrderResult:
        del symbol
        return OrderResult(ok=True, order_id=None, status="no position",
                           filled_qty=0.0, raw={})

    def get_position(self, symbol: str) -> Position | None:
        del symbol
        return None

    def health_check(self) -> bool:
        return True


class _NoFetchVenue(_AckOnlyVenue):
    """A venue with no order-status support at all."""

    fetch_order = None  # type: ignore[assignment]


class _Strategy:
    def on_bar(self, candles):
        del candles
        from tradingbot.models import Action, OrderType, PositionSide, Signal
        return Signal(
            strategy="s", action=Action.buy, symbol="BTC/USD",
            order_type=OrderType.market, quantity=0.1,
            position_side=PositionSide.long,
        )


class _Store:
    def __init__(self) -> None:
        self.trades: list[tuple[str, dict]] = []

    def append_trade(self, bot_id: str, record: dict) -> None:
        self.trades.append((bot_id, record))


def _config(bot_id: str = "one") -> BotConfig:
    return BotConfig(
        id=bot_id, venue="coinbase", market_type="spot", strategy="example",
        symbol="BTC/USD", timeframe="1m", quantity=0.1, live=True,
        per_bot_cap=1_000.0, global_cap=10_000.0, params={},
    )


async def _started(monkeypatch, venue, store) -> BotSupervisor:
    monkeypatch.setattr(
        "tradingbot.service.supervisor.build_venue", lambda *a, **k: venue
    )
    monkeypatch.setattr(
        "tradingbot.service.supervisor.build_strategy", lambda *a, **k: _Strategy()
    )
    supervisor = BotSupervisor(
        hub_factory=lambda cfg: _Hub(),
        event_bus=EventBus(),
        exposure=ExposureTracker(),
        store=store,
    )
    supervisor.create(_config())
    await supervisor.start("one")
    # Let the bar flow through the lane so an order is actually submitted.
    for _ in range(20):
        await asyncio.sleep(0)
        if store.trades:
            break
    return supervisor


def _kinds(store: _Store) -> list[str | None]:
    return [record.get("kind") for _, record in store.trades]


@pytest.mark.asyncio
async def test_an_acknowledged_order_starts_out_open(monkeypatch) -> None:
    """Baseline: without polling, the order stays submitted forever."""
    store = _Store()
    supervisor = await _started(monkeypatch, _AckOnlyVenue(), store)
    bot = supervisor.get("one")
    assert bot is not None

    assert _kinds(store) == ["submitted"]
    (order,) = bot.ledger.open_orders()
    assert order.state is OrderState.submitted

    await supervisor.stop("one")


@pytest.mark.asyncio
async def test_polling_advances_an_order_to_filled(monkeypatch) -> None:
    store = _Store()
    venue = _AckOnlyVenue(
        OrderResult(ok=True, order_id="v1", status="closed",
                    filled_qty=0.1, raw={"average": 100.0})
    )
    supervisor = await _started(monkeypatch, venue, store)
    bot = supervisor.get("one")
    assert bot is not None

    await supervisor.reconcile_open_orders(bot)

    assert venue.fetched == ["v1"]
    assert "order_status" in _kinds(store)
    (order,) = bot.ledger.orders()
    assert order.state is OrderState.filled
    assert order.filled_qty == pytest.approx(0.1)

    await supervisor.stop("one")


@pytest.mark.asyncio
async def test_a_filled_order_is_not_polled_again(monkeypatch) -> None:
    """Terminal orders leave the worklist, so polling cost stays bounded."""
    store = _Store()
    venue = _AckOnlyVenue(
        OrderResult(ok=True, order_id="v1", status="closed",
                    filled_qty=0.1, raw={"average": 100.0})
    )
    supervisor = await _started(monkeypatch, venue, store)
    bot = supervisor.get("one")
    assert bot is not None

    await supervisor.reconcile_open_orders(bot)
    await supervisor.reconcile_open_orders(bot)

    assert venue.fetched == ["v1"], "a terminal order must not be re-polled"

    await supervisor.stop("one")


@pytest.mark.asyncio
async def test_repeated_polls_do_not_double_count(monkeypatch) -> None:
    """The commit-1b invariant, end to end through the supervisor."""
    store = _Store()
    venue = _AckOnlyVenue(
        OrderResult(ok=True, order_id="v1", status="open",
                    filled_qty=0.05, raw={"average": 100.0})
    )
    supervisor = await _started(monkeypatch, venue, store)
    bot = supervisor.get("one")
    assert bot is not None

    for _ in range(3):
        await supervisor.reconcile_open_orders(bot)

    (order,) = bot.ledger.orders()
    assert order.filled_qty == pytest.approx(0.05), "cumulative, not summed"
    assert _kinds(store).count("order_status") == 1, "unchanged polls write nothing"

    await supervisor.stop("one")


@pytest.mark.asyncio
async def test_a_failing_poll_leaves_the_order_open(monkeypatch) -> None:
    """A venue error is not evidence. The order must stay live in our books."""
    store = _Store()
    supervisor = await _started(monkeypatch, _AckOnlyVenue(status=None), store)
    bot = supervisor.get("one")
    assert bot is not None

    await supervisor.reconcile_open_orders(bot)

    (order,) = bot.ledger.open_orders()
    assert order.state is OrderState.submitted
    assert "rejected" not in _kinds(store)
    assert "canceled" not in _kinds(store)

    await supervisor.stop("one")


@pytest.mark.asyncio
async def test_a_venue_without_status_support_is_skipped(monkeypatch) -> None:
    """Not every venue can answer. That must not break the poll loop."""
    store = _Store()
    supervisor = await _started(monkeypatch, _NoFetchVenue(), store)
    bot = supervisor.get("one")
    assert bot is not None

    await supervisor.reconcile_open_orders(bot)

    (order,) = bot.ledger.open_orders()
    assert order.state is OrderState.submitted

    await supervisor.stop("one")


@pytest.mark.asyncio
async def test_a_cancelled_order_records_its_partial_fill(monkeypatch) -> None:
    store = _Store()
    venue = _AckOnlyVenue(
        OrderResult(ok=True, order_id="v1", status="canceled",
                    filled_qty=0.04, raw={"average": 100.0})
    )
    supervisor = await _started(monkeypatch, venue, store)
    bot = supervisor.get("one")
    assert bot is not None

    await supervisor.reconcile_open_orders(bot)

    (order,) = bot.ledger.orders()
    assert order.state is OrderState.canceled
    assert order.filled_qty == pytest.approx(0.04)

    await supervisor.stop("one")


class _ReplayStore:
    """Store double holding a pre-existing lifecycle log."""

    def __init__(self, events: list[dict], configs: list[BotConfig] | None = None) -> None:
        self.events = list(events)
        self.configs = configs or [_config()]
        self.appended: list[tuple[str, dict]] = []

    def load_configs(self) -> list[BotConfig]:
        return list(self.configs)

    def replay_trades(self, bot_id: str):
        del bot_id
        return iter(self.events)

    def append_trade(self, bot_id: str, record: dict) -> None:
        self.appended.append((bot_id, record))


def _supervisor(store) -> BotSupervisor:
    return BotSupervisor(
        hub_factory=lambda cfg: _Hub(),
        event_bus=EventBus(),
        exposure=ExposureTracker(),
        store=store,
    )


def _submitted(coid: str, *, venue_order_id: str | None = "v1", qty: float = 2.0) -> dict:
    return {
        "kind": "submitted", "client_order_id": coid, "bot_id": "one",
        "symbol": "BTC/USD", "side": "buy", "order_type": "market",
        "qty": qty, "price": None, "venue_order_id": venue_order_id, "ts": 1,
    }


def test_restore_rebuilds_the_ledger_from_the_log() -> None:
    """A restart must not forget orders that were live when it happened."""
    store = _ReplayStore([
        _submitted("c1"),
        {"kind": "order_status", "client_order_id": "c1", "filled_qty": 2.0,
         "avg_price": 100.0, "ts": 2},
        _submitted("c2", venue_order_id="v2"),
    ])
    supervisor = _supervisor(store)

    assert supervisor.restore() == 1
    bot = supervisor.get("one")
    assert bot is not None

    assert {o.client_order_id for o in bot.ledger.orders()} == {"c1", "c2"}
    filled = bot.ledger.order("c1")
    assert filled is not None
    assert filled.state is OrderState.filled
    # c2 was still live when the process died, so it must come back as work.
    assert [o.client_order_id for o in bot.ledger.open_orders()] == ["c2"]


def test_restore_survives_a_corrupt_log_row() -> None:
    """One bad record must not cost the whole history -- #108's lesson."""
    store = _ReplayStore([
        _submitted("c1"),
        {"kind": "nonsense"},
        _submitted("c2", venue_order_id="v2"),
    ])
    supervisor = _supervisor(store)
    supervisor.restore()
    bot = supervisor.get("one")
    assert bot is not None

    assert {o.client_order_id for o in bot.ledger.orders()} == {"c1", "c2"}


def test_restore_survives_a_store_that_cannot_replay() -> None:
    """A replay failure must not stop the service booting."""
    class _Broken(_ReplayStore):
        def replay_trades(self, bot_id: str):
            raise OSError("disk gone")

    supervisor = _supervisor(_Broken([]))

    assert supervisor.restore() == 1
    bot = supervisor.get("one")
    assert bot is not None
    assert bot.ledger.orders() == []


def test_restore_does_not_rewrite_the_log_it_just_read() -> None:
    """Replay is a read. Folding it must not append anything."""
    store = _ReplayStore([_submitted("c1")])
    supervisor = _supervisor(store)
    supervisor.restore()

    assert store.appended == []


def test_a_legacy_row_is_ignored_by_the_ledger_replay() -> None:
    """Pre-#135 rows have no `kind` and carry no order identity."""
    store = _ReplayStore([
        {"bot_id": "one", "action": "buy", "status": "filled", "ok": True,
         "order_id": "o1", "symbol": "BTC/USD", "ts": 1},
        _submitted("c1"),
    ])
    supervisor = _supervisor(store)
    supervisor.restore()
    bot = supervisor.get("one")
    assert bot is not None

    assert [o.client_order_id for o in bot.ledger.orders()] == ["c1"]
