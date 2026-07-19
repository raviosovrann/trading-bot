"""Bounded off-loop execution for synchronous exchange calls.

The venue and candle-feed interfaces are synchronous (ccxt is a blocking HTTP
client), but the service runs one asyncio event loop. Calling them inline means
a single slow exchange freezes every API request, every WebSocket, and every
other bot.

Everything here exists to keep that from happening:

* **Off the loop.** Calls run on worker threads, so the loop stays free.
* **Bounded.** Each pool has a fixed worker count, so a misbehaving venue
  cannot spawn threads without limit.
* **Isolated per venue.** :class:`WorkerPools` hands out one pool per name, so
  a stuck venue exhausts only its own workers and never another venue's.
* **Time-limited.** Every call has a deadline, so a hung socket does not pin a
  caller forever.

A caveat worth knowing: a timeout abandons the *wait*, not the thread. Python
cannot interrupt a blocked C call, so the worker stays busy until the
underlying socket gives up. That is why pools are per venue — a venue that
hangs every call degrades only itself.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

_log = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_CALL_TIMEOUT = 20.0
"""Seconds a single exchange call may take before the caller gives up.

Generous on purpose: exchanges are routinely slow under load, and a timeout
here abandons a call that may already have reached the venue.
"""

DEFAULT_MAX_WORKERS = 4
"""Concurrent blocking calls allowed per venue."""


class BlockingCallTimeout(TimeoutError):
    """Raised when a synchronous exchange call overruns its deadline."""


class BlockingCalls:
    """A named, bounded thread pool for synchronous calls, with a deadline."""

    def __init__(
        self,
        name: str,
        *,
        max_workers: int = DEFAULT_MAX_WORKERS,
        timeout: float = DEFAULT_CALL_TIMEOUT,
    ) -> None:
        """Initialize the pool.

        Args:
            name: Identifier used in thread names and log messages.
            max_workers: Concurrent calls allowed.
            timeout: Seconds before a call is abandoned.

        Raises:
            ValueError: If ``max_workers`` or ``timeout`` is not positive.
        """
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._name = name
        self._timeout = timeout
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=f"venue-{name}"
        )

    @property
    def name(self) -> str:
        """Return the pool's name."""
        return self._name

    @property
    def timeout(self) -> float:
        """Return the per-call deadline in seconds."""
        return self._timeout

    async def run(self, fn: Callable[..., T], *args: Any) -> T:
        """Run ``fn(*args)`` on a worker thread and await its result.

        Args:
            fn: Synchronous callable to execute.
            args: Positional arguments for ``fn``.

        Returns:
            Whatever ``fn`` returns.

        Raises:
            BlockingCallTimeout: If the call overruns the pool's deadline.
            Exception: Anything ``fn`` raises.
        """
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._pool, functools.partial(fn, *args))
        try:
            return await asyncio.wait_for(future, self._timeout)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            # The worker thread keeps running until the socket gives up; we
            # only stop waiting on it.
            _log.warning(
                "%s: blocking call %s exceeded %.1fs and was abandoned",
                self._name,
                getattr(fn, "__name__", repr(fn)),
                self._timeout,
            )
            raise BlockingCallTimeout(
                f"{self._name}: call exceeded {self._timeout:.1f}s"
            ) from exc

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        """Run ``fn(*args)`` on a worker without awaiting the result.

        For work that must leave the event loop immediately and has no caller
        to return to — a stream callback, for instance. Failures are logged
        rather than lost, since nothing awaits the future.

        Args:
            fn: Synchronous callable to execute.
            args: Positional arguments for ``fn``.
        """
        try:
            future = self._pool.submit(fn, *args)
        except RuntimeError:  # pragma: no cover - pool already shut down
            _log.warning("%s: dropped work submitted after shutdown", self._name)
            return
        future.add_done_callback(self._log_failure)

    def _log_failure(self, future: Any) -> None:
        """Log an exception from fire-and-forget work.

        Args:
            future: The completed future.
        """
        if future.cancelled():
            return
        error = future.exception()
        if error is not None:
            _log.exception("%s: background call failed", self._name, exc_info=error)

    def shutdown(self, wait: bool = True) -> None:
        """Shut the pool down.

        Args:
            wait: Whether to block until running calls finish. Pass ``False``
                when a call is known to be hung.
        """
        self._pool.shutdown(wait=wait, cancel_futures=True)


class WorkerPools:
    """Hands out one :class:`BlockingCalls` pool per name, creating on demand.

    Keyed by venue so one exchange's slowness is contained: its calls queue
    behind its own workers while every other venue keeps running.
    """

    def __init__(
        self,
        *,
        max_workers: int = DEFAULT_MAX_WORKERS,
        timeout: float = DEFAULT_CALL_TIMEOUT,
    ) -> None:
        """Initialize an empty registry.

        Args:
            max_workers: Concurrent calls allowed per pool.
            timeout: Seconds before a call is abandoned.
        """
        self._max_workers = max_workers
        self._timeout = timeout
        self._pools: dict[str, BlockingCalls] = {}
        self._lock = threading.Lock()

    def for_name(self, name: str) -> BlockingCalls:
        """Return the pool for ``name``, creating it on first use.

        Args:
            name: Venue or subsystem identifier.

        Returns:
            The pool dedicated to ``name``.
        """
        with self._lock:
            pool = self._pools.get(name)
            if pool is None:
                pool = BlockingCalls(
                    name, max_workers=self._max_workers, timeout=self._timeout
                )
                self._pools[name] = pool
            return pool

    def shutdown(self, wait: bool = False) -> None:
        """Shut every pool down.

        Args:
            wait: Whether to block until running calls finish.
        """
        with self._lock:
            pools = list(self._pools.values())
            self._pools.clear()
        for pool in pools:
            pool.shutdown(wait=wait)
