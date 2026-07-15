"""Edge-case tests for the in-memory event bus."""

from __future__ import annotations

import asyncio

import pytest

from tradingbot.service.events import EventBus, OrderEvent


@pytest.mark.asyncio
async def test_subscribe_outside_event_loop_creates_queue_without_loop() -> None:
    """A queue can be created before an event loop is running."""
    bus = EventBus()
    queue = bus.subscribe()
    assert queue is not None
    assert bus.subscriber_count() == 1


def test_unsubscribe_unknown_queue_is_silent() -> None:
    """Removing a queue that was never subscribed is a no-op."""
    bus = EventBus()
    queue: asyncio.Queue[object] = asyncio.Queue()
    bus.unsubscribe(queue)
    assert bus.subscriber_count() == 0
