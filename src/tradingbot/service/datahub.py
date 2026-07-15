from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

from ..datafeed import CandleFeed
from ..models import Candle
from ..stream import StreamingFeed
from .ratelimit import RateLimiter

_log = logging.getLogger(__name__)
_Key = tuple[str, str]
_Handler = Callable[[Candle], None]
_StreamRunner = Callable[..., Awaitable[None]]


class MarketDataHub:
    """Share candle warmups and stream subscriptions between bot instances."""

    def __init__(
        self,
        *,
        stream_feed: StreamingFeed,
        candle_feed: CandleFeed,
        limiter: RateLimiter,
        mtf_cache_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
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
        self._latest_prices: dict[_Key, float] = {}
        self._warmup_lock = asyncio.Lock()

    def subscribe(self, symbol: str, timeframe: str, handler: _Handler) -> None:
        key = (symbol, timeframe)
        handlers = self._handlers.setdefault(key, [])
        if handler in handlers:
            return
        handlers.append(handler)
        if len(handlers) > 1:
            return

        self._stream_feed.on_bar(
            lambda candle, subscription_key=key: self._on_bar(subscription_key, candle),
        )
        runner = getattr(self._stream_feed, "run_async", None)
        if not callable(runner):
            raise RuntimeError("MarketDataHub requires a stream feed with run_async")
        self._stream_tasks[key] = asyncio.create_task(
            self._run_stream(key, cast(_StreamRunner, runner)),
        )

    def unsubscribe(self, symbol: str, timeframe: str, handler: _Handler) -> None:
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

    async def _run_stream(self, key: _Key, runner: _StreamRunner) -> None:
        try:
            await runner(key[0])
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("market data stream stopped for %s %s", *key)
        finally:
            current = asyncio.current_task()
            if self._stream_tasks.get(key) is current:
                self._stream_tasks.pop(key, None)

    def _on_bar(self, key: _Key, candle: Candle) -> None:
        self._latest_prices[key] = candle.close
        for handler in tuple(self._handlers.get(key, ())):
            try:
                handler(candle)
            except Exception:
                _log.exception("market data handler failed for %s %s", *key)

    async def warmup(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        key = (symbol, timeframe)
        async with self._warmup_lock:
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
        return self._latest_prices.get((symbol, timeframe))
