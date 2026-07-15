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
        """Initialize a token bucket.

        Args:
            rate_per_sec: Tokens refilled per second. Must be positive.
            burst: Maximum tokens the bucket can hold. Must be positive.
            clock: Callable returning the current time in seconds.
            sleep: Async sleep callable used to wait for tokens.

        Raises:
            ValueError: If ``rate_per_sec`` or ``burst`` is not positive.
        """
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
        while True:
            async with self._lock:
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
                # Release the state lock while waiting so another waiter can
                # observe refilled tokens and make progress independently.
            await self._sleep(wait_seconds)
