from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from .datafeed import CcxtCandleFeed, _ohlcv_to_candle
from .models import Candle

try:
    import ccxt.pro as ccxtpro  # type: ignore
except Exception:  # pragma: no cover - optional third-party install
    ccxtpro = None  # type: ignore[assignment]


class StreamingFeed(Protocol):
    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]: ...
    def on_bar(self, handler: Callable[[Candle], None]) -> None: ...
    def run(self, *symbols: str) -> None: ...
    def stop(self) -> None: ...


class CcxtStreamFeed:
    """Event-driven push feed backed by ccxt.pro's async ``watch_ohlcv``.

    ccxt.pro is async-only, so the sync ``run()`` wraps the watch loop in
    ``asyncio.run``. Each ``watch_ohlcv`` update returns recent OHLCV rows with
    the final row still forming; only rows strictly newer than the last emitted
    timestamp — excluding that forming bar — are handed to the handler, so a
    candle is emitted exactly once, when it closes. Warmup history comes over
    REST via an injected sync ``CcxtCandleFeed``.
    """

    def __init__(
        self,
        exchange: Any | None = None,
        warmup_feed: Any | None = None,
        timeframe: str = "1m",
    ) -> None:
        if exchange is None:
            raise ValueError("CcxtStreamFeed requires an exchange or use from_exchange(...)")
        self._ex = exchange
        self._warmup_feed = warmup_feed
        self._timeframe = timeframe
        self._handler: Callable[[Candle], None] | None = None
        self._last_ts: int | None = None
        self._stopped = False
        self._lock = threading.Lock()

    @classmethod
    def from_exchange(
        cls,
        exchange_id: str,
        api_key: str,
        api_secret: str,
        password: str | None = None,
        timeframe: str = "1m",
    ) -> "CcxtStreamFeed":
        if ccxtpro is None:
            raise RuntimeError("ccxt.pro is not installed")
        klass = getattr(ccxtpro, exchange_id)
        config: dict[str, Any] = {"apiKey": api_key, "secret": api_secret, "enableRateLimit": True}
        if password:
            config["password"] = password
        warmup_feed = CcxtCandleFeed.from_exchange(exchange_id, api_key, api_secret, password)
        return cls(exchange=klass(config), warmup_feed=warmup_feed, timeframe=timeframe)

    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        if self._warmup_feed is None:
            raise RuntimeError("CcxtStreamFeed has no warmup feed configured")
        return self._warmup_feed.warmup_candles(symbol, timeframe, limit)

    def on_bar(self, handler: Callable[[Candle], None]) -> None:
        self._handler = handler

    def _on_candles(self, candles: list) -> None:
        """Emit newly-closed candles, dropping the forming final row and dupes."""
        with self._lock:
            for row in candles[:-1]:  # last row is the still-forming candle
                ts = int(row[0])
                if self._last_ts is not None and ts <= self._last_ts:
                    continue
                self._last_ts = ts
                handler = self._handler
                if handler is not None:
                    handler(_ohlcv_to_candle(row))

    async def _watch_loop(self, symbol: str) -> None:
        try:
            while not self._stopped:
                candles = await self._ex.watch_ohlcv(symbol, self._timeframe)
                if candles:
                    self._on_candles(candles)
        finally:
            await self._ex.close()

    def run(self, *symbols: str) -> None:
        if not symbols:
            raise ValueError("CcxtStreamFeed.run requires a symbol")
        asyncio.run(self._watch_loop(symbols[0]))

    def stop(self) -> None:
        # Idempotent: signal the watch loop to exit after its current message.
        self._stopped = True


def run_with_reconnect(
    *,
    connect_and_run: Callable[[], None],
    should_stop: Callable[[], bool],
    gap_fill: Callable[[], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    base_backoff: float = 1.0,
    max_backoff: float = 60.0,
) -> None:
    """Supervise a blocking stream connection, reconnecting on drop.

    ``connect_and_run`` blocks while connected and returns (or raises) on
    disconnect. Between attempts we sleep with exponential backoff: it grows on
    connect failures and resets to ``base_backoff`` after a healthy connection
    drops, capped at ``max_backoff``. ``gap_fill`` (if given) runs after each
    backoff to REST-fill bars missed during the outage before reconnecting.
    """
    backoff = base_backoff
    while not should_stop():
        try:
            connect_and_run()
            healthy = True
        except Exception:
            healthy = False
        if should_stop():
            break
        if healthy:
            backoff = base_backoff
        sleep(backoff)
        if should_stop():
            break
        if gap_fill is not None:
            gap_fill()
        if not healthy:
            backoff = min(backoff * 2, max_backoff)
