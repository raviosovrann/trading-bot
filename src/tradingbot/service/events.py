from __future__ import annotations

import asyncio
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any


DEFAULT_QUEUE_MAXSIZE = 256
"""Events buffered per subscriber before the drop policy engages.

Sized so a browser that stalls for a few seconds still catches up losslessly,
while a client that has effectively stopped reading cannot pin unbounded memory.
"""


@dataclass
class DecisionEvent:
    """A strategy decision emitted by a running bot."""

    bot_id: str
    symbol: str
    ts: int
    text: str


@dataclass
class OrderEvent:
    """An order placement result emitted by a running bot."""

    bot_id: str
    action: str
    status: str
    ok: bool
    order_id: str | None


@dataclass
class BotStateEvent:
    """An authoritative snapshot of one bot's runtime state.

    Carries the whole view rather than a delta so a subscriber can apply it
    without refetching, and so a dropped frame costs nothing: the next event
    is still complete. ``seq`` increases strictly per bot, letting a client
    discard a snapshot that arrives after a newer one.
    """

    bot_id: str
    seq: int
    status: str
    position: dict[str, Any] | None
    pnl: float
    last_decision: str | None
    degraded: bool = False
    degraded_reason: str | None = None


@dataclass
class OverflowEvent:
    """Tells a subscriber that events were dropped and it must resynchronize.

    Emitted instead of silently discarding: the client's live view is now
    incomplete, so it should refetch the authoritative state and the paginated
    order history rather than trust what it has.
    """

    dropped: int


class EventSubscription:
    """A bounded, per-subscriber event buffer with a typed drop policy.

    Rather than an unbounded queue, each subscriber holds at most ``maxsize``
    events and sheds load by kind when it falls behind:

    * :class:`BotStateEvent` **coalesces** — a newer snapshot for the same bot
      replaces the queued one. Snapshots are complete, so this loses nothing.
    * :class:`DecisionEvent` is **droppable** — it is an informational tick in
      a rolling log, and the oldest is shed first to make room.
    * :class:`OrderEvent` is **never shed to make room for something else**. It
      is also durably persisted to the trade log, so if the buffer is so full
      that even an order cannot land, dropping it is reported rather than
      hidden and the client recovers from the history endpoint.

    Any drop raises an :class:`OverflowEvent`, delivered ahead of the buffered
    events, so overflow is always observable to the client.
    """

    def __init__(self, maxsize: int = DEFAULT_QUEUE_MAXSIZE) -> None:
        """Initialize an empty subscription.

        Args:
            maxsize: Maximum events buffered before the drop policy engages.

        Raises:
            ValueError: If ``maxsize`` is not positive.
        """
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self._maxsize = maxsize
        self._items: deque[Any] = deque()
        self._lock = threading.Lock()
        self._dropped = 0
        self._overflow_pending = False
        self._waiter: asyncio.Event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def offer(self, event: Any) -> None:
        """Buffer ``event``, applying the drop policy when full.

        Safe to call from any thread.

        Args:
            event: Event to deliver to this subscriber.
        """
        with self._lock:
            self._offer_locked(event)
        self._wake()

    def _offer_locked(self, event: Any) -> None:
        """Apply the drop policy and buffer ``event``. Caller holds the lock.

        Args:
            event: Event to deliver to this subscriber.
        """
        # Only shed under pressure. Coalescing a subscriber that is keeping up
        # would hide real transitions (starting -> running) for no benefit.
        if len(self._items) < self._maxsize:
            self._items.append(event)
            return
        # Full. A newer snapshot can displace the queued one for its bot.
        if isinstance(event, BotStateEvent) and self._supersede_locked(event):
            return
        if self._shed_a_decision_locked():
            self._items.append(event)
            return
        # Nothing sheddable: this subscriber is hopelessly behind. Drop the
        # incoming event and tell it to resynchronize.
        self._dropped += 1
        self._overflow_pending = True

    def _supersede_locked(self, event: BotStateEvent) -> bool:
        """Replace a queued snapshot for the same bot with ``event``.

        Args:
            event: The newer snapshot.

        Returns:
            ``True`` if an older snapshot was replaced.
        """
        for index, queued in enumerate(self._items):
            if isinstance(queued, BotStateEvent) and queued.bot_id == event.bot_id:
                if queued.seq <= event.seq:
                    self._items[index] = event
                return True
        return False

    def _shed_a_decision_locked(self) -> bool:
        """Drop the oldest decision event to free a slot.

        Returns:
            ``True`` if a slot was freed.
        """
        for index, queued in enumerate(self._items):
            if isinstance(queued, DecisionEvent):
                del self._items[index]
                self._dropped += 1
                self._overflow_pending = True
                return True
        return False

    def _wake(self) -> None:
        """Wake a pending ``get`` from any thread."""
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._waiter.set)
                return
            except RuntimeError:  # pragma: no cover - loop closed under us
                pass
        self._waiter.set()

    async def get(self) -> Any:
        """Return the next event, waiting until one is available.

        An :class:`OverflowEvent` takes priority, so a client learns its view
        is stale before it acts on anything that follows.

        Returns:
            The next event for this subscriber.
        """
        while True:
            with self._lock:
                if self._overflow_pending:
                    self._overflow_pending = False
                    dropped, self._dropped = self._dropped, 0
                    return OverflowEvent(dropped=dropped)
                if self._items:
                    return self._items.popleft()
                self._waiter.clear()
            await self._waiter.wait()

    def get_nowait(self) -> Any:
        """Return the next event without waiting.

        Returns:
            The next event for this subscriber.

        Raises:
            asyncio.QueueEmpty: If there is nothing to deliver.
        """
        with self._lock:
            if self._overflow_pending:
                self._overflow_pending = False
                dropped, self._dropped = self._dropped, 0
                return OverflowEvent(dropped=dropped)
            if self._items:
                return self._items.popleft()
        raise asyncio.QueueEmpty

    def qsize(self) -> int:
        """Return the number of buffered events."""
        with self._lock:
            return len(self._items)

    def empty(self) -> bool:
        """Return whether there is nothing to deliver."""
        with self._lock:
            return not self._items and not self._overflow_pending

    @property
    def dropped(self) -> int:
        """Return the number of events dropped since the last overflow report."""
        with self._lock:
            return self._dropped


class EventBus:
    """In-memory fan-out bus for bot events.

    Each subscriber receives a private, **bounded** :class:`EventSubscription`.
    ``publish`` is safe to call from a different thread than the subscriber's
    event loop, and never blocks on a slow subscriber.
    """

    def __init__(self) -> None:
        """Initialize an empty bus."""
        self._subscriptions: set[EventSubscription] = set()
        self._lock = threading.Lock()

    def publish(self, event: Any) -> None:
        """Offer ``event`` to every subscriber, applying each one's drop policy."""
        with self._lock:
            subscriptions = tuple(self._subscriptions)
        for subscription in subscriptions:
            subscription.offer(event)

    def subscribe(self, maxsize: int = DEFAULT_QUEUE_MAXSIZE) -> EventSubscription:
        """Create and register a new bounded subscription.

        Args:
            maxsize: Maximum events buffered before the drop policy engages.

        Returns:
            An :class:`EventSubscription` receiving subsequently published events.
        """
        subscription = EventSubscription(maxsize=maxsize)
        with self._lock:
            self._subscriptions.add(subscription)
        return subscription

    def unsubscribe(self, subscription: EventSubscription) -> None:
        """Remove ``subscription`` from the bus."""
        with self._lock:
            self._subscriptions.discard(subscription)

    def subscriber_count(self) -> int:
        """Return the number of active subscriptions."""
        with self._lock:
            return len(self._subscriptions)
