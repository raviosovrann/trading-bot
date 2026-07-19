"""Tests for bounded event fan-out and overflow reporting (#122)."""

from __future__ import annotations

import asyncio

import pytest

from tradingbot.service.events import (
    BotStateEvent,
    DecisionEvent,
    EventBus,
    OrderEvent,
    OverflowEvent,
)


def _state(bot_id: str = "b1", seq: int = 1, pnl: float = 0.0) -> BotStateEvent:
    return BotStateEvent(
        bot_id=bot_id,
        seq=seq,
        status="running",
        position=None,
        pnl=pnl,
        last_decision=None,
    )


def _decision(n: int = 0) -> DecisionEvent:
    return DecisionEvent(bot_id="b1", symbol="BTC/USD", ts=n, text=f"tick {n}")


def _order(order_id: str = "o1") -> OrderEvent:
    return OrderEvent(bot_id="b1", action="buy", status="filled", ok=True, order_id=order_id)


async def _drain(sub) -> list:
    """Return everything currently buffered on ``sub``."""
    await asyncio.sleep(0)
    out = []
    while not sub.empty():
        out.append(await sub.get())
    return out


@pytest.mark.asyncio
async def test_slow_subscriber_memory_is_bounded() -> None:
    """Verify a subscriber that never reads cannot grow without bound."""
    bus = EventBus()
    sub = bus.subscribe(maxsize=8)

    for i in range(1000):
        bus.publish(_decision(i))
    await asyncio.sleep(0)

    assert sub.qsize() <= 8


@pytest.mark.asyncio
async def test_state_events_coalesce_per_bot() -> None:
    """Verify a newer snapshot supersedes the queued one for the same bot.

    State events carry the whole authoritative view (#114), so replacing an
    undelivered one loses nothing.
    """
    bus = EventBus()
    sub = bus.subscribe(maxsize=2)

    bus.publish(_state("b1", seq=1, pnl=1.0))
    bus.publish(_state("b2", seq=1, pnl=2.0))
    bus.publish(_state("b1", seq=2, pnl=3.0))
    await asyncio.sleep(0)

    events = await _drain(sub)
    states = [e for e in events if isinstance(e, BotStateEvent)]
    assert not any(isinstance(e, OverflowEvent) for e in events), "coalescing must not overflow"
    by_bot = {e.bot_id: e for e in states}
    assert by_bot["b1"].seq == 2
    assert by_bot["b1"].pnl == 3.0
    assert by_bot["b2"].pnl == 2.0


@pytest.mark.asyncio
async def test_decisions_are_dropped_before_orders() -> None:
    """Verify a full queue sheds informational decisions to keep an order."""
    bus = EventBus()
    sub = bus.subscribe(maxsize=3)

    bus.publish(_decision(1))
    bus.publish(_decision(2))
    bus.publish(_decision(3))
    bus.publish(_order("keep-me"))
    await asyncio.sleep(0)

    events = await _drain(sub)
    orders = [e for e in events if isinstance(e, OrderEvent)]
    assert [o.order_id for o in orders] == ["keep-me"]


@pytest.mark.asyncio
async def test_overflow_is_reported_to_the_subscriber() -> None:
    """Verify dropping is observable, so a client knows to refetch."""
    bus = EventBus()
    sub = bus.subscribe(maxsize=2)

    for i in range(10):
        bus.publish(_order(f"o{i}"))
    await asyncio.sleep(0)

    events = await _drain(sub)
    overflows = [e for e in events if isinstance(e, OverflowEvent)]
    assert overflows, "a dropped event must be reported, not silently lost"
    assert overflows[0].dropped > 0


@pytest.mark.asyncio
async def test_overflow_is_reported_once_per_burst() -> None:
    """Verify the overflow notice does not itself spam the subscriber."""
    bus = EventBus()
    sub = bus.subscribe(maxsize=2)

    for i in range(50):
        bus.publish(_order(f"o{i}"))
    await asyncio.sleep(0)
    first = await _drain(sub)
    assert len([e for e in first if isinstance(e, OverflowEvent)]) == 1

    # Nothing further published: no new overflow notice.
    assert await _drain(sub) == []


@pytest.mark.asyncio
async def test_a_reading_subscriber_loses_nothing() -> None:
    """Verify the bound never costs a subscriber that keeps up."""
    bus = EventBus()
    sub = bus.subscribe(maxsize=4)
    received: list = []

    for i in range(20):
        bus.publish(_order(f"o{i}"))
        await asyncio.sleep(0)
        while not sub.empty():
            received.append(await sub.get())

    assert [e.order_id for e in received if isinstance(e, OrderEvent)] == [
        f"o{i}" for i in range(20)
    ]
    assert not any(isinstance(e, OverflowEvent) for e in received)


@pytest.mark.asyncio
async def test_get_blocks_until_an_event_arrives() -> None:
    """Verify ``get`` waits rather than spinning when the buffer is empty."""
    bus = EventBus()
    sub = bus.subscribe(maxsize=4)

    getter = asyncio.create_task(sub.get())
    await asyncio.sleep(0)
    assert not getter.done()

    bus.publish(_order("later"))
    event = await asyncio.wait_for(getter, timeout=1.0)
    assert isinstance(event, OrderEvent)


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery() -> None:
    """Verify an unsubscribed reader receives nothing further."""
    bus = EventBus()
    sub = bus.subscribe(maxsize=4)

    bus.unsubscribe(sub)
    bus.publish(_order("o1"))
    await asyncio.sleep(0)

    assert sub.empty()
    assert bus.subscriber_count() == 0
