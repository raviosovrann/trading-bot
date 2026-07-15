from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any


@dataclass
class DecisionEvent:
    bot_id: str
    symbol: str
    ts: int
    text: str


@dataclass
class OrderEvent:
    bot_id: str
    action: str
    status: str
    ok: bool
    order_id: str | None


class EventBus:
    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[Any]] = set()
        self._loops: dict[asyncio.Queue[Any], asyncio.AbstractEventLoop] = {}
        self._lock = threading.Lock()

    def publish(self, event: Any) -> None:
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
        with self._lock:
            self._queues.discard(queue)
            self._loops.pop(queue, None)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._queues)
