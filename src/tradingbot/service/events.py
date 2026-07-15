from __future__ import annotations

import asyncio
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

    def publish(self, event: Any) -> None:
        for queue in tuple(self._queues):
            queue.put_nowait(event)

    def subscribe(self) -> asyncio.Queue[Any]:
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Any]) -> None:
        self._queues.discard(queue)
