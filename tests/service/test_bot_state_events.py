"""Tests for authoritative bot-state broadcasting (#114)."""

from __future__ import annotations

import asyncio

import pytest

from tradingbot.models import Action, Candle, Order, OrderResult, OrderType, Position, PositionSide, Signal
from tradingbot.service.events import BotStateEvent, EventBus, EventSubscription
from tradingbot.service.risk import GlobalExposure
from tradingbot.service.supervisor import BotConfig, BotSupervisor


def _candle(ts: int = 1, close: float = 100.0) -> Candle:
    return Candle(timestamp=ts, open=close, high=close, low=close, close=close, volume=1.0)


class _FakeHub:
    """Hub double that records stream listeners and can fire them."""

    def __init__(self) -> None:
        self.handlers: dict[tuple[str, str], list] = {}
        self.listeners: list = []
        self.price: float | None = 100.0

    async def warmup(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        del symbol, timeframe, limit
        return [_candle()]

    def subscribe(self, symbol: str, timeframe: str, handler) -> None:
        self.handlers.setdefault((symbol, timeframe), []).append(handler)

    def unsubscribe(self, symbol: str, timeframe: str, handler) -> None:
        self.handlers[(symbol, timeframe)].remove(handler)

    def latest_price(self, symbol: str, timeframe: str) -> float | None:
        del symbol, timeframe
        return self.price

    def add_stream_listener(self, listener) -> None:
        self.listeners.append(listener)

    def remove_stream_listener(self, listener) -> None:
        if listener in self.listeners:
            self.listeners.remove(listener)

    def fire_stream_exit(self, symbol: str, timeframe: str, reason: str) -> None:
        for listener in tuple(self.listeners):
            listener(symbol, timeframe, reason)


class _FakeVenue:
    def __init__(self) -> None:
        self.position: Position | None = None

    def place_order(self, order: Order) -> OrderResult:
        return OrderResult(ok=True, order_id="order-1", status="filled", filled_qty=order.qty, raw={})

    def close_position(self, symbol: str) -> OrderResult:
        del symbol
        return OrderResult(ok=True, order_id=None, status="no position", filled_qty=0.0, raw={})

    def get_position(self, symbol: str) -> Position | None:
        del symbol
        return self.position

    def health_check(self) -> bool:
        return True


class _SignalStrategy:
    def on_bar(self, candles) -> Signal | None:
        del candles
        return Signal(
            strategy="test",
            action=Action.buy,
            symbol="BTC/USD",
            order_type=OrderType.market,
            quantity=0.1,
            position_side=PositionSide.long,
        )


class _IdleStrategy:
    def on_bar(self, candles) -> Signal | None:
        del candles
        return None


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
    )


async def _drain(queue: EventSubscription) -> list[BotStateEvent]:
    """Return every queued state event, discarding other event types.

    The bus hands events to the loop with ``call_soon_threadsafe``, so a tick
    is needed before anything published this iteration is visible.
    """
    await asyncio.sleep(0)
    events: list[BotStateEvent] = []
    while not queue.empty():
        event = queue.get_nowait()
        if isinstance(event, BotStateEvent):
            events.append(event)
    return events


def _supervisor(
    monkeypatch,
    *,
    hub: _FakeHub,
    venue: _FakeVenue,
    strategy=None,
    poll_seconds: float = 60.0,
) -> BotSupervisor:
    monkeypatch.setattr("tradingbot.service.supervisor.build_venue", lambda *a, **k: venue)
    monkeypatch.setattr(
        "tradingbot.service.supervisor.build_strategy",
        lambda *a, **k: strategy if strategy is not None else _IdleStrategy(),
    )
    return BotSupervisor(
        hub_factory=lambda cfg: hub,
        event_bus=EventBus(),
        global_exposure=GlobalExposure(),
        state_poll_seconds=poll_seconds,
    )


@pytest.mark.asyncio
async def test_create_publishes_initial_state() -> None:
    """Verify creating a bot broadcasts its initial ``created`` state."""
    bus = EventBus()
    supervisor = BotSupervisor(
        hub_factory=lambda cfg: _FakeHub(),
        event_bus=bus,
        global_exposure=GlobalExposure(),
    )
    queue = bus.subscribe()

    supervisor.create(_config())

    events = await _drain(queue)
    assert [e.status for e in events] == ["created"]
    assert events[0].bot_id == "bot-1"
    assert events[0].pnl == 0.0
    assert events[0].degraded is False


@pytest.mark.asyncio
async def test_start_and_stop_publish_every_lifecycle_transition(monkeypatch) -> None:
    """Verify start/stop broadcast starting, running, stopping and stopped."""
    hub, venue = _FakeHub(), _FakeVenue()
    supervisor = _supervisor(monkeypatch, hub=hub, venue=venue)
    supervisor.create(_config())
    queue = supervisor.event_bus.subscribe()

    await supervisor.start("bot-1")
    await supervisor.stop("bot-1")

    # The runtime also emits its own startup message, which republishes a
    # (changed) running snapshot; collapse repeats and assert the transitions.
    statuses = [e.status for e in await _drain(queue)]
    collapsed = [s for i, s in enumerate(statuses) if i == 0 or s != statuses[i - 1]]
    assert collapsed == ["starting", "running", "stopping", "stopped"]


@pytest.mark.asyncio
async def test_failed_start_publishes_failed_state(monkeypatch) -> None:
    """Verify a start that raises broadcasts a ``failed`` state, not silence."""
    hub, venue = _FakeHub(), _FakeVenue()
    supervisor = _supervisor(monkeypatch, hub=hub, venue=venue)
    monkeypatch.setattr(
        "tradingbot.service.supervisor.build_venue",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("venue down")),
    )
    supervisor.create(_config())
    queue = supervisor.event_bus.subscribe()

    with pytest.raises(RuntimeError):
        await supervisor.start("bot-1")

    assert [e.status for e in await _drain(queue)] == ["starting", "failed"]


@pytest.mark.asyncio
async def test_runtime_crash_publishes_failed_state(monkeypatch) -> None:
    """Verify a runtime task dying broadcasts ``failed`` without a refetch."""
    hub, venue = _FakeHub(), _FakeVenue()
    supervisor = _supervisor(monkeypatch, hub=hub, venue=venue)
    bot = supervisor.create(_config())
    await supervisor.start("bot-1")
    queue = supervisor.event_bus.subscribe()

    # Simulate the runtime task exiting with an error, as _task_done sees it.
    async def _boom() -> None:
        raise RuntimeError("stream runtime died")

    task = asyncio.create_task(_boom())
    bot.task = task
    task.add_done_callback(lambda t: supervisor._task_done(bot, t))
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)

    assert bot.status == "failed"
    statuses = [e.status for e in await _drain(queue)]
    assert statuses[-1] == "failed"
    await supervisor.stop("bot-1")


@pytest.mark.asyncio
async def test_state_events_carry_a_monotonic_sequence(monkeypatch) -> None:
    """Verify every state event for a bot carries a strictly increasing seq."""
    hub, venue = _FakeHub(), _FakeVenue()
    supervisor = _supervisor(monkeypatch, hub=hub, venue=venue)
    queue = supervisor.event_bus.subscribe()

    supervisor.create(_config())
    await supervisor.start("bot-1")
    await supervisor.stop("bot-1")

    seqs = [e.seq for e in await _drain(queue)]
    assert seqs == sorted(set(seqs))
    assert len(seqs) >= 5


@pytest.mark.asyncio
async def test_poll_publishes_pnl_change_without_an_order(monkeypatch) -> None:
    """Verify the bounded-cadence poll broadcasts PnL moves with no fill."""
    hub, venue = _FakeHub(), _FakeVenue()
    venue.position = Position(symbol="BTC/USD", side=PositionSide.long, size=2.0, entry_price=100.0)
    supervisor = _supervisor(monkeypatch, hub=hub, venue=venue, poll_seconds=0.01)
    supervisor.create(_config())
    await supervisor.start("bot-1")
    queue = supervisor.event_bus.subscribe()

    hub.price = 110.0
    deadline = asyncio.get_running_loop().time() + 2.0
    events: list[BotStateEvent] = []
    while asyncio.get_running_loop().time() < deadline and not events:
        await asyncio.sleep(0.01)
        events = [e for e in await _drain(queue) if e.pnl != 0.0]

    await supervisor.stop("bot-1")
    assert events, "expected a polled PnL update"
    assert events[0].pnl == pytest.approx(20.0)
    assert events[0].position is not None
    assert events[0].position["side"] == "long"


@pytest.mark.asyncio
async def test_poll_is_silent_when_nothing_changed(monkeypatch) -> None:
    """Verify the poll does not spam identical snapshots onto the bus."""
    hub, venue = _FakeHub(), _FakeVenue()
    supervisor = _supervisor(monkeypatch, hub=hub, venue=venue, poll_seconds=0.01)
    supervisor.create(_config())
    await supervisor.start("bot-1")
    queue = supervisor.event_bus.subscribe()

    # ~10 poll ticks with nothing moving.
    await asyncio.sleep(0.1)
    await supervisor.stop("bot-1")

    statuses = [e.status for e in await _drain(queue)]
    assert statuses.count("running") <= 1, f"poll republished an unchanged snapshot: {statuses}"
    assert statuses[-2:] == ["stopping", "stopped"]


@pytest.mark.asyncio
async def test_unexpected_stream_exit_marks_the_bot_degraded(monkeypatch) -> None:
    """Verify a stream dying under a running bot broadcasts a degraded state."""
    hub, venue = _FakeHub(), _FakeVenue()
    supervisor = _supervisor(monkeypatch, hub=hub, venue=venue)
    bot = supervisor.create(_config())
    await supervisor.start("bot-1")
    queue = supervisor.event_bus.subscribe()

    hub.fire_stream_exit("BTC/USD", "1m", "connection reset")

    # The runtime's own startup message may also republish a running snapshot;
    # assert on the degradation itself rather than on frame counts.
    degraded_events = [e for e in await _drain(queue) if e.degraded]
    assert degraded_events, "the stream exit must be broadcast"
    assert {e.status for e in degraded_events} == {"running"}
    assert "connection reset" in (degraded_events[0].degraded_reason or "")
    assert bot.status == "running", "degradation must stay distinct from failed"
    assert bot.degraded is True
    await supervisor.stop("bot-1")


@pytest.mark.asyncio
async def test_stream_exit_for_another_symbol_is_ignored(monkeypatch) -> None:
    """Verify a shared hub only degrades the bots on the affected symbol."""
    hub, venue = _FakeHub(), _FakeVenue()
    supervisor = _supervisor(monkeypatch, hub=hub, venue=venue)
    bot = supervisor.create(_config())
    await supervisor.start("bot-1")
    queue = supervisor.event_bus.subscribe()

    hub.fire_stream_exit("ETH/USD", "1m", "connection reset")

    assert [e for e in await _drain(queue) if e.degraded] == []
    assert bot.degraded is False
    await supervisor.stop("bot-1")


@pytest.mark.asyncio
async def test_restart_clears_degradation_and_unregisters_listener(monkeypatch) -> None:
    """Verify stopping detaches the hub listener and restarting clears degraded."""
    hub, venue = _FakeHub(), _FakeVenue()
    supervisor = _supervisor(monkeypatch, hub=hub, venue=venue)
    bot = supervisor.create(_config())
    await supervisor.start("bot-1")
    hub.fire_stream_exit("BTC/USD", "1m", "connection reset")
    assert bot.degraded is True

    await supervisor.stop("bot-1")
    assert hub.listeners == [], "a stopped bot must not keep a hub listener"

    await supervisor.start("bot-1")
    assert bot.degraded is False
    assert bot.degraded_reason is None
    await supervisor.stop("bot-1")
