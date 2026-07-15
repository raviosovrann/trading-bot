from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class RateLimiter:
    """A token bucket that limits asynchronous calls to a shared account."""

    def __init__(
        self,
        rate_per_sec: float,
        burst: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be greater than zero")
        if burst <= 0:
            raise ValueError("burst must be greater than zero")

        self._rate_per_sec = rate_per_sec
        self._burst = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._tokens = self._burst
        self._updated_at = clock()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until one token is available, then consume it."""
        async with self._lock:
            while True:
                now = self._clock()
                elapsed = max(0.0, now - self._updated_at)
                self._tokens = min(
                    self._burst,
                    self._tokens + elapsed * self._rate_per_sec,
                )
                self._updated_at = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return

                wait_seconds = (1.0 - self._tokens) / self._rate_per_sec
                await self._sleep(wait_seconds)
