from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any


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


class EventBus:
    """In-memory fan-out bus for bot events.

    Subscribers receive a private ``asyncio.Queue``. ``publish`` is safe to
    call from a different thread than the subscriber's event loop.
    """

    def __init__(self) -> None:
        """Initialize an empty bus."""
        self._queues: set[asyncio.Queue[Any]] = set()
        self._loops: dict[asyncio.Queue[Any], asyncio.AbstractEventLoop] = {}
        self._lock = threading.Lock()

    def publish(self, event: Any) -> None:
        """Enqueue ``event`` on every subscriber queue."""
        with self._lock:
            queues = tuple(self._queues)
            loops = dict(self._loops)
        for queue in queues:
            loop = loops.get(queue)
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(queue.put_nowait, event)
            else:
                queue.put_nowait(event)

    def subscribe(self) -> asyncio.Queue[Any]:
        """Create and register a new subscriber queue.

        Returns:
            An ``asyncio.Queue`` that will receive every published event.
        """
        queue: asyncio.Queue[Any] = asyncio.Queue()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        with self._lock:
            self._queues.add(queue)
            if loop is not None:
                self._loops[queue] = loop
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Any]) -> None:
        """Remove ``queue`` from the bus."""
        with self._lock:
            self._queues.discard(queue)
            self._loops.pop(queue, None)

    def subscriber_count(self) -> int:
        """Return the number of active subscriber queues."""
        with self._lock:
            return len(self._queues)
