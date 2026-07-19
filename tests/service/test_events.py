"""Edge-case tests for the in-memory event bus."""

from __future__ import annotations

import pytest

from tradingbot.service.events import EventBus, EventSubscription, OrderEvent


@pytest.mark.asyncio
async def test_subscribe_outside_event_loop_creates_queue_without_loop() -> None:
    """A queue can be created before an event loop is running."""
    bus = EventBus()
    queue = bus.subscribe()
    assert queue is not None
    assert bus.subscriber_count() == 1


def test_unsubscribe_unknown_subscription_is_silent() -> None:
    """Removing a subscription that was never registered is a no-op."""
    bus = EventBus()
    bus.unsubscribe(EventSubscription())
    assert bus.subscriber_count() == 0


def test_subscribe_rejects_a_non_positive_bound() -> None:
    """A zero-size buffer could never deliver anything, so it is refused."""
    with pytest.raises(ValueError):
        EventBus().subscribe(maxsize=0)
