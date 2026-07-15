from __future__ import annotations

import asyncio

import pytest

from tradingbot.models import Action, Candle, Order, OrderResult, OrderType, Position, PositionSide, Signal
from tradingbot.service.events import EventBus, OrderEvent
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
    # Wait until the subscription is registered before publishing.
    for _ in range(200):
        if bus.subscriber_count() > 0:
            break
        await asyncio.sleep(0.01)
    assert bus.subscriber_count() > 0, "subscriber was not registered"
    bus.publish(event)
    await sub_task
    assert received == [event]


@pytest.mark.asyncio
async def test_supervisor_start_stop_and_order_event(monkeypatch) -> None:
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

    event = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert isinstance(event, OrderEvent)
    assert event.bot_id == "one"
    assert event.action == "buy"

    await supervisor.stop("one")
    assert supervisor.get("one").status == "stopped"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_two_bots_run_concurrently(monkeypatch) -> None:
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
