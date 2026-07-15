from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import cast

from ..datafeed import CandleFeed
from ..models import Candle
from ..stream import StreamingFeed
from .ratelimit import RateLimiter

_log = logging.getLogger(__name__)
_Key = tuple[str, str]
_Handler = Callable[[Candle], None]
_StreamRunner = Callable[..., Awaitable[None]]


class MarketDataHub:
    """Share candle warmups and stream subscriptions between bot instances.

    One hub per venue. Identical subscriptions share a single underlying
    stream, and warmup/MTF fetches are rate-limited and cached so N bots on
    the same symbol/timeframe cause O(1) exchange requests.
    """

    def __init__(
        self,
        *,
        stream_feed: StreamingFeed,
        candle_feed: CandleFeed,
        limiter: RateLimiter,
        mtf_cache_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize the hub.

        Args:
            stream_feed: Push-based streaming feed for live bars.
            candle_feed: REST-backed feed for historical warmups.
            limiter: Shared rate limiter for all REST calls.
            mtf_cache_seconds: How long cached warmups remain valid.
            clock: Time source, injected for deterministic tests.

        Raises:
            ValueError: If ``mtf_cache_seconds`` is negative.
        """
        if mtf_cache_seconds < 0:
            raise ValueError("mtf_cache_seconds must not be negative")

        self._stream_feed = stream_feed
        self._candle_feed = candle_feed
        self._limiter = limiter
        self._mtf_cache_seconds = mtf_cache_seconds
        self._clock = clock
        self._handlers: dict[_Key, list[_Handler]] = {}
        self._stream_tasks: dict[_Key, asyncio.Task[None]] = {}
        self._warmup_cache: dict[_Key, tuple[float, list[Candle]]] = {}
        self._warmup_locks: dict[_Key, asyncio.Lock] = {}
        self._latest_prices: dict[_Key, float] = {}

    def subscribe(self, symbol: str, timeframe: str, handler: _Handler) -> None:
        """Register ``handler`` for closed candles on ``symbol/timeframe``.

        The first subscriber starts the stream; later subscribers on the same
        key are added to the fan-out list without starting another stream.
        """
        key = (symbol, timeframe)
        handlers = self._handlers.setdefault(key, [])
        if handler in handlers:
            return
        handlers.append(handler)
        if len(handlers) > 1:
            return

        handler = lambda candle, subscription_key=key: self._on_bar(subscription_key, candle)
        keyed_register = getattr(self._stream_feed, "on_bar_for", None)
        if callable(keyed_register):
            keyed_register(symbol, handler)
        elif len(self._handlers) == 1:
            self._stream_feed.on_bar(handler)
        else:
            self._handlers.pop(key)
            raise RuntimeError("MarketDataHub requires keyed stream handlers for multiple symbols")
        self._stream_tasks[key] = asyncio.create_task(self._run_stream(key))

    def unsubscribe(self, symbol: str, timeframe: str, handler: _Handler) -> None:
        """Remove ``handler`` from ``symbol/timeframe``.

        When the last handler for a key is removed, the stream task is cancelled
        and, if no keys remain, the underlying feed is stopped.
        """
        key = (symbol, timeframe)
        handlers = self._handlers.get(key)
        if handlers is None or handler not in handlers:
            return

        handlers.remove(handler)
        if handlers:
            return

        self._handlers.pop(key, None)
        task = self._stream_tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()
        if not self._handlers:
            self._stream_feed.stop()

    async def _run_stream(self, key: _Key) -> None:
        """Run the underlying stream for ``key`` until cancelled."""
        try:
            runner = getattr(self._stream_feed, "run_async", None)
            if callable(runner):
                await cast(_StreamRunner, runner)(key[0])
            else:
                legacy_runner = getattr(self._stream_feed, "run", None)
                if not callable(legacy_runner):
                    raise RuntimeError("MarketDataHub requires stream feed run_async() or run()")
                await asyncio.to_thread(legacy_runner, key[0])
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("market data stream stopped for %s %s", *key)
        finally:
            try:
                current = asyncio.current_task()
            except RuntimeError:  # pragma: no cover - event loop is closing
                return
            if self._stream_tasks.get(key) is current:
                self._stream_tasks.pop(key, None)

    def _on_bar(self, key: _Key, candle: Candle) -> None:
        """Fan a closed candle out to all handlers for ``key``."""
        self._latest_prices[key] = candle.close
        for handler in tuple(self._handlers.get(key, ())):
            try:
                handler(candle)
            except Exception:
                _log.exception("market data handler failed for %s %s", *key)

    async def warmup(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        """Return up to ``limit`` historical candles, rate-limited and cached.

        Concurrent calls for the same key coalesce behind an async lock so only
        one REST fetch is made; subsequent callers receive the cached copy until
        ``mtf_cache_seconds`` elapses.
        """
        key = (symbol, timeframe)
        lock = self._warmup_locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = self._clock()
            cached = self._warmup_cache.get(key)
            if cached is not None and now - cached[0] < self._mtf_cache_seconds:
                return list(cached[1])

            await self._limiter.acquire()
            candles = list(self._candle_feed.warmup_candles(symbol, timeframe, limit))
            self._warmup_cache[key] = (self._clock(), candles)
            if candles:
                self._latest_prices[key] = candles[-1].close
            return list(candles)

    def latest_price(self, symbol: str, timeframe: str) -> float | None:
        """Return the last close seen for ``symbol/timeframe`` (or ``None``)."""
        return self._latest_prices.get((symbol, timeframe))
