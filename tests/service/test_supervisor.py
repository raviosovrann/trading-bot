"""Tests for the bot supervisor and event bus."""

from __future__ import annotations

import asyncio
import json

import pytest

from tradingbot.models import Action, Candle, Order, OrderResult, OrderType, Position, PositionSide, Signal
from tradingbot.service.events import EventBus, OrderEvent
from tradingbot.router import SignalRouter
from tradingbot.service.risk import GlobalExposure
from tradingbot.service.supervisor import BotConfig, BotSupervisor


def _candle(ts: int = 1, close: float = 100.0) -> Candle:
    return Candle(timestamp=ts, open=close, high=close, low=close, close=close, volume=1.0)


class _FakeHub:
    def __init__(self) -> None:
        self.handlers: dict[tuple[str, str], list] = {}
        self.warmups = 0

    async def warmup(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        del symbol, timeframe, limit
        self.warmups += 1
        return [_candle()]

    def subscribe(self, symbol: str, timeframe: str, handler) -> None:
        self.handlers.setdefault((symbol, timeframe), []).append(handler)

    def unsubscribe(self, symbol: str, timeframe: str, handler) -> None:
        self.handlers[(symbol, timeframe)].remove(handler)

    def latest_price(self, symbol: str, timeframe: str) -> float | None:
        del symbol, timeframe
        return 100.0


class _FakeVenue:
    def place_order(self, order: Order) -> OrderResult:
        return OrderResult(ok=True, order_id="order-1", status="filled", filled_qty=order.qty, raw={})

    def close_position(self, symbol: str) -> OrderResult:
        del symbol
        return OrderResult(ok=True, order_id=None, status="no position", filled_qty=0.0, raw={})

    def get_position(self, symbol: str) -> Position | None:
        del symbol
        return None

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


def _config(bot_id: str) -> BotConfig:
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


@pytest.mark.asyncio
async def test_event_bus_fans_out_and_unsubscribes() -> None:
    """Verify that the event bus fans out events and supports unsubscribing."""
    bus = EventBus()
    first = bus.subscribe()
    second = bus.subscribe()
    event = OrderEvent(bot_id="bot", action="buy", status="filled", ok=True, order_id="1")

    bus.publish(event)

    assert await first.get() is event
    assert await second.get() is event
    bus.unsubscribe(second)
    bus.publish(event)
    assert await first.get() is event
    assert second.empty()


@pytest.mark.asyncio
async def test_event_bus_concurrent_subscribe_and_publish() -> None:
    """Verify that concurrent subscribe and publish on the event bus works correctly."""
    bus = EventBus()
    received: list[OrderEvent] = []

    async def subscriber() -> None:
        queue = bus.subscribe()
        try:
            received.append(await asyncio.wait_for(queue.get(), timeout=1.0))
        finally:
            bus.unsubscribe(queue)

    event = OrderEvent(bot_id="bot", action="buy", status="filled", ok=True, order_id="1")
    sub_task = asyncio.create_task(subscriber())
    # Yield once so the subscriber task can register.
    await asyncio.sleep(0)
    deadline = asyncio.get_running_loop().time() + 1.0
    while bus.subscriber_count() == 0 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0)
    assert bus.subscriber_count() > 0
    bus.publish(event)
    await sub_task
    assert received == [event]


@pytest.mark.asyncio
async def test_supervisor_start_stop_and_order_event(monkeypatch) -> None:
    """Verify that the supervisor can start/stop a bot and emits an order event."""
    hub = _FakeHub()
    bus = EventBus()
    monkeypatch.setattr("tradingbot.service.supervisor.build_venue", lambda *args, **kwargs: _FakeVenue())
    monkeypatch.setattr("tradingbot.service.supervisor.build_strategy", lambda *args, **kwargs: _SignalStrategy())
    supervisor = BotSupervisor(
        hub_factory=lambda cfg: hub,
        event_bus=bus,
        global_exposure=GlobalExposure(),
    )
    supervisor.create(_config("one"))
    queue = bus.subscribe()

    await supervisor.start("one")
    assert supervisor.get("one").status == "running"  # type: ignore[union-attr]

    # State snapshots (#114) share the bus, so skip past them to the order.
    async def _next_order() -> OrderEvent:
        while True:
            event = await asyncio.wait_for(queue.get(), timeout=1.0)
            if isinstance(event, OrderEvent):
                return event

    order = await asyncio.wait_for(_next_order(), timeout=2.0)
    assert order.bot_id == "one"
    assert order.action == "buy"

    await supervisor.stop("one")
    assert supervisor.get("one").status == "stopped"  # type: ignore[union-attr]


class _RecordingStore:
    def __init__(self) -> None:
        self.trades: list[tuple[str, dict]] = []

    def append_trade(self, bot_id: str, order_event: dict) -> None:
        self.trades.append((bot_id, order_event))


@pytest.mark.asyncio
async def test_supervisor_persists_order_events(monkeypatch) -> None:
    """Order events are appended to the store, not only published to the bus."""
    bus = EventBus()
    store = _RecordingStore()
    monkeypatch.setattr("tradingbot.service.supervisor.build_venue", lambda *a, **k: _FakeVenue())
    monkeypatch.setattr("tradingbot.service.supervisor.build_strategy", lambda *a, **k: _SignalStrategy())
    supervisor = BotSupervisor(
        hub_factory=lambda cfg: _FakeHub(),
        event_bus=bus,
        global_exposure=GlobalExposure(),
        store=store,
    )
    supervisor.create(_config("one"))
    queue = bus.subscribe()

    await supervisor.start("one")
    await asyncio.wait_for(queue.get(), timeout=1.0)  # wait until the order fired
    await supervisor.stop("one")

    assert store.trades, "expected the order event to be persisted"
    bot_id, record = store.trades[0]
    assert bot_id == "one"
    assert record["action"] == "buy"
    assert record["bot_id"] == "one"


class _PosVenue:
    def __init__(self, pos: Position | None) -> None:
        self._pos = pos

    def get_position(self, symbol: str) -> Position | None:
        del symbol
        return self._pos


class _PriceHub:
    def __init__(self, price: float | None) -> None:
        self._price = price

    def latest_price(self, symbol: str, timeframe: str) -> float | None:
        del symbol, timeframe
        return self._price


def _order_json() -> str:
    return json.dumps({
        "type": "order", "action": "buy", "status": "submitted",
        "ok": True, "order_id": "o1", "symbol": "BTC/USD", "ts": 1,
    })


def test_order_event_refreshes_position_and_marks_long_pnl() -> None:
    """On an order event the supervisor reads the venue position and marks PnL."""
    supervisor = BotSupervisor(
        hub_factory=lambda cfg: _FakeHub(), event_bus=EventBus(), global_exposure=GlobalExposure()
    )
    bot = supervisor.create(_config("one"))
    bot.venue = _PosVenue(Position(symbol="BTC/USD", side=PositionSide.long, size=2.0, entry_price=100.0))
    bot.hub = _PriceHub(110.0)
    bot.multiplier = 1.0

    supervisor._handle_event(bot, _order_json())

    assert bot.position is not None and bot.position.side is PositionSide.long
    assert bot.pnl == 20.0  # (110 - 100) * 2 * 1


def test_short_position_marks_inverse_pnl() -> None:
    """A short position profits when price falls below entry."""
    supervisor = BotSupervisor(
        hub_factory=lambda cfg: _FakeHub(), event_bus=EventBus(), global_exposure=GlobalExposure()
    )
    bot = supervisor.create(_config("one"))
    bot.venue = _PosVenue(Position(symbol="BTC/USD", side=PositionSide.short, size=2.0, entry_price=100.0))
    bot.hub = _PriceHub(90.0)
    bot.multiplier = 1.0

    supervisor._handle_event(bot, _order_json())

    assert bot.pnl == 20.0  # (100 - 90) * 2 * 1


def test_flat_position_zeroes_pnl() -> None:
    """When the venue reports no position, PnL resets to zero."""
    supervisor = BotSupervisor(
        hub_factory=lambda cfg: _FakeHub(), event_bus=EventBus(), global_exposure=GlobalExposure()
    )
    bot = supervisor.create(_config("one"))
    bot.pnl = 5.0
    bot.venue = _PosVenue(None)
    bot.hub = _PriceHub(100.0)

    supervisor._handle_event(bot, _order_json())

    assert bot.position is None
    assert bot.pnl == 0.0


@pytest.mark.asyncio
async def test_two_bots_run_concurrently(monkeypatch) -> None:
    """Verify that two bots can start and run concurrently."""
    hubs = {}
    monkeypatch.setattr("tradingbot.service.supervisor.build_venue", lambda *args, **kwargs: _FakeVenue())
    monkeypatch.setattr("tradingbot.service.supervisor.build_strategy", lambda *args, **kwargs: _SignalStrategy())

    def make_hub(cfg: BotConfig) -> _FakeHub:
        hubs[cfg.id] = _FakeHub()
        return hubs[cfg.id]

    supervisor = BotSupervisor(
        hub_factory=make_hub,
        event_bus=EventBus(),
        global_exposure=GlobalExposure(),
    )
    supervisor.create(_config("one"))
    supervisor.create(_config("two"))

    await asyncio.gather(supervisor.start("one"), supervisor.start("two"))
    assert [bot.status for bot in supervisor.list()] == ["running", "running"]
    await asyncio.gather(supervisor.stop("one"), supervisor.stop("two"))


class _FakeStore:
    """Minimal store double exposing only what the supervisor restores from."""

    def __init__(self, configs: list[BotConfig]) -> None:
        self._configs = list(configs)

    def load_configs(self) -> list[BotConfig]:
        return list(self._configs)


def test_restore_loads_persisted_configs_into_supervisor() -> None:
    """Every persisted config is adopted by the supervisor on restore."""
    store = _FakeStore([_config("one"), _config("two")])
    supervisor = BotSupervisor(
        hub_factory=lambda cfg: _FakeHub(),
        event_bus=EventBus(),
        global_exposure=GlobalExposure(),
        store=store,
    )

    restored = supervisor.restore()

    assert restored == 2
    assert sorted(bot.config.id for bot in supervisor.list()) == ["one", "two"]


def test_restored_bots_are_not_running() -> None:
    """Restored bots sit in a safe non-running state until explicitly started."""
    store = _FakeStore([_config("one")])
    supervisor = BotSupervisor(
        hub_factory=lambda cfg: _FakeHub(),
        event_bus=EventBus(),
        global_exposure=GlobalExposure(),
        store=store,
    )

    supervisor.restore()

    bot = supervisor.get("one")
    assert bot is not None
    assert bot.status == "stopped"
    assert bot.task is None
    assert bot.runtime is None


def test_restore_does_not_clobber_existing_bots() -> None:
    """Restoring twice is a no-op for ids the supervisor already manages."""
    store = _FakeStore([_config("one")])
    supervisor = BotSupervisor(
        hub_factory=lambda cfg: _FakeHub(),
        event_bus=EventBus(),
        global_exposure=GlobalExposure(),
        store=store,
    )
    supervisor.restore()
    supervisor.get("one").status = "running"  # type: ignore[union-attr]

    assert supervisor.restore() == 0
    assert supervisor.get("one").status == "running"  # type: ignore[union-attr]
    assert len(supervisor.list()) == 1


def test_restore_without_store_is_a_no_op() -> None:
    """A supervisor with no store restores nothing rather than failing."""
    supervisor = BotSupervisor(
        hub_factory=lambda cfg: _FakeHub(),
        event_bus=EventBus(),
        global_exposure=GlobalExposure(),
    )

    assert supervisor.restore() == 0
    assert supervisor.list() == []


class _SlowHub(_FakeHub):
    """Hub whose warmup blocks until released, to widen the start window."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def warmup(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        await self.release.wait()
        return await super().warmup(symbol, timeframe, limit)


def _lifecycle_supervisor(monkeypatch, hub_factory) -> BotSupervisor:
    monkeypatch.setattr("tradingbot.service.supervisor.build_venue", lambda *a, **k: _FakeVenue())
    monkeypatch.setattr("tradingbot.service.supervisor.build_strategy", lambda *a, **k: _SignalStrategy())
    return BotSupervisor(
        hub_factory=hub_factory,
        event_bus=EventBus(),
        global_exposure=GlobalExposure(),
    )


@pytest.mark.asyncio
async def test_concurrent_starts_create_exactly_one_runtime(monkeypatch) -> None:
    """Two simultaneous starts must not build two runtimes for one bot."""
    hub = _SlowHub()
    supervisor = _lifecycle_supervisor(monkeypatch, lambda cfg: hub)
    supervisor.create(_config("one"))

    # Both starts must be in flight before either completes, otherwise the
    # race this guards against never happens.
    first = asyncio.create_task(supervisor.start("one"))
    second = asyncio.create_task(supervisor.start("one"))
    await asyncio.sleep(0)
    hub.release.set()
    await asyncio.gather(first, second)

    bot = supervisor.get("one")
    assert bot is not None
    assert hub.warmups == 1, "second start rebuilt the runtime"
    assert len(hub.handlers[("BTC/USD", "1m")]) == 1, "duplicate market subscription"
    assert bot.status == "running"
    await supervisor.stop("one")


@pytest.mark.asyncio
async def test_start_while_starting_does_not_orphan_a_task(monkeypatch) -> None:
    """A start issued mid-startup joins the in-flight one instead of racing it."""
    hub = _SlowHub()
    supervisor = _lifecycle_supervisor(monkeypatch, lambda cfg: hub)
    supervisor.create(_config("one"))

    first = asyncio.create_task(supervisor.start("one"))
    await asyncio.sleep(0)  # let the first start reach the blocking warmup
    bot = supervisor.get("one")
    assert bot is not None
    assert bot.status == "starting", "no transitional state while starting"

    second = asyncio.create_task(supervisor.start("one"))
    hub.release.set()
    await asyncio.gather(first, second)

    assert hub.warmups == 1
    assert bot.status == "running"
    await supervisor.stop("one")


@pytest.mark.asyncio
async def test_concurrent_stops_clean_up_exactly_once(monkeypatch) -> None:
    """Simultaneous stops leave one clean shutdown and no orphan task."""
    hub = _SlowHub()
    hub.release.set()
    supervisor = _lifecycle_supervisor(monkeypatch, lambda cfg: hub)
    supervisor.create(_config("one"))
    await supervisor.start("one")
    bot = supervisor.get("one")
    assert bot is not None
    task = bot.task

    await asyncio.gather(supervisor.stop("one"), supervisor.stop("one"))

    assert bot.status == "stopped"
    assert task is not None and task.done()
    assert hub.handlers[("BTC/USD", "1m")] == [], "subscription outlived the stop"


@pytest.mark.asyncio
async def test_stop_is_idempotent_for_a_bot_that_never_started(monkeypatch) -> None:
    """Stopping a created bot is a no-op, not an error."""
    supervisor = _lifecycle_supervisor(monkeypatch, lambda cfg: _FakeHub())
    supervisor.create(_config("one"))

    await supervisor.stop("one")
    await supervisor.stop("one")

    bot = supervisor.get("one")
    assert bot is not None and bot.status == "stopped"


@pytest.mark.asyncio
async def test_failed_start_releases_resources_and_allows_retry(monkeypatch) -> None:
    """A start that blows up mid-build leaves nothing behind and can be retried."""
    hub = _FakeHub()
    calls = {"n": 0}

    def flaky_strategy(*args, **kwargs):
        # Fail *after* the hub, venue and multiplier have been attached, so the
        # test proves those partial resources get released.
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("strategy params rejected")
        return _SignalStrategy()

    monkeypatch.setattr("tradingbot.service.supervisor.build_venue", lambda *a, **k: _FakeVenue())
    monkeypatch.setattr("tradingbot.service.supervisor.build_strategy", flaky_strategy)
    supervisor = BotSupervisor(
        hub_factory=lambda cfg: hub,
        event_bus=EventBus(),
        global_exposure=GlobalExposure(),
    )
    supervisor.create(_config("one"))

    with pytest.raises(RuntimeError):
        await supervisor.start("one")

    bot = supervisor.get("one")
    assert bot is not None
    assert bot.status == "failed"
    assert bot.task is None and bot.runtime is None
    assert bot.venue is None and bot.hub is None

    await supervisor.start("one")
    assert bot.status == "running"
    await supervisor.stop("one")


@pytest.mark.asyncio
async def test_stop_during_start_waits_and_leaves_the_bot_stopped(monkeypatch) -> None:
    """A stop racing a start serializes behind it rather than half-cancelling."""
    hub = _SlowHub()
    supervisor = _lifecycle_supervisor(monkeypatch, lambda cfg: hub)
    supervisor.create(_config("one"))

    start = asyncio.create_task(supervisor.start("one"))
    await asyncio.sleep(0)
    stop = asyncio.create_task(supervisor.stop("one"))
    await asyncio.sleep(0)
    hub.release.set()
    await asyncio.gather(start, stop)

    bot = supervisor.get("one")
    assert bot is not None
    assert bot.status == "stopped"
    assert bot.task is None or bot.task.done()
    assert hub.handlers[("BTC/USD", "1m")] == []


class _LiveAwareVenue(_FakeVenue):
    """Venue that remembers whether it was built for live trading."""

    def __init__(self, live: bool) -> None:
        self.live = live


def _recording_supervisor(monkeypatch, seen: dict) -> BotSupervisor:
    """Supervisor whose venue/risk/strategy construction is recorded."""

    def record_venue(venue, market_type, *, creds, live):
        del venue, market_type, creds
        seen["venue_live"] = live
        return _LiveAwareVenue(live)

    def record_strategy(name, context):
        del name
        seen["params"] = context.params
        seen["quantity"] = context.quantity
        return _SignalStrategy()

    real_guard = SignalRouter.with_risk_guard

    def record_guard(venue, **kwargs):
        seen["per_bot_cap"] = kwargs.get("per_bot_cap")
        seen["global_cap"] = kwargs.get("global_cap")
        return real_guard(venue, **kwargs)

    monkeypatch.setattr("tradingbot.service.supervisor.build_venue", record_venue)
    monkeypatch.setattr("tradingbot.service.supervisor.build_strategy", record_strategy)
    monkeypatch.setattr(
        "tradingbot.service.supervisor.SignalRouter.with_risk_guard", staticmethod(record_guard)
    )
    return BotSupervisor(
        hub_factory=lambda cfg: _FakeHub(),
        event_bus=EventBus(),
        global_exposure=GlobalExposure(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("live", [True, False])
async def test_live_flag_reaches_the_venue_at_start(monkeypatch, live: bool) -> None:
    """The venue is built live or dry-run to match the config, both ways."""
    seen: dict = {}
    supervisor = _recording_supervisor(monkeypatch, seen)
    cfg = _config("one")
    cfg.live = live
    supervisor.create(cfg)

    await supervisor.start("one")

    assert seen["venue_live"] is live
    bot = supervisor.get("one")
    assert bot is not None and bot.venue is not None
    assert bot.venue.live is live, "the active venue disagrees with the config"
    await supervisor.stop("one")


@pytest.mark.asyncio
async def test_caps_and_params_reach_the_risk_guard_and_strategy_at_start(monkeypatch) -> None:
    """Risk caps and strategy params are taken from the config when starting."""
    seen: dict = {}
    supervisor = _recording_supervisor(monkeypatch, seen)
    cfg = _config("one")
    cfg.per_bot_cap = 250.0
    cfg.global_cap = 2_500.0
    cfg.params = {"fast": 5}
    supervisor.create(cfg)

    await supervisor.start("one")

    assert seen["per_bot_cap"] == 250.0
    assert seen["global_cap"] == 2_500.0
    assert seen["params"] == {"fast": 5}
    await supervisor.stop("one")


@pytest.mark.asyncio
async def test_mutating_config_while_running_does_not_reach_the_venue(monkeypatch) -> None:
    """The hazard the API's 409 exists to prevent, pinned as a regression.

    The venue, risk guard and strategy are constructed once at start. Flipping
    ``config.live`` afterwards changes only the advertised value — the live
    venue keeps its original mode. This is why a patch on a running bot must be
    refused rather than silently accepted.
    """
    seen: dict = {}
    supervisor = _recording_supervisor(monkeypatch, seen)
    cfg = _config("one")
    cfg.live = False
    supervisor.create(cfg)
    await supervisor.start("one")
    bot = supervisor.get("one")
    assert bot is not None and bot.venue is not None

    bot.config.live = True  # what an unguarded PATCH used to do

    assert bot.venue.live is False, "venue silently changed mode"
    await supervisor.stop("one")


@pytest.mark.asyncio
async def test_config_change_takes_effect_after_a_restart(monkeypatch) -> None:
    """Stop, edit, start is the supported path and genuinely rebuilds."""
    seen: dict = {}
    supervisor = _recording_supervisor(monkeypatch, seen)
    cfg = _config("one")
    cfg.live = False
    supervisor.create(cfg)
    await supervisor.start("one")
    await supervisor.stop("one")

    cfg.live = True
    cfg.per_bot_cap = 42.0
    await supervisor.start("one")

    assert seen["venue_live"] is True
    assert seen["per_bot_cap"] == 42.0
    bot = supervisor.get("one")
    assert bot is not None and bot.venue is not None and bot.venue.live is True
    await supervisor.stop("one")
