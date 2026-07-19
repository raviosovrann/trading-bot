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
    async def run_async(self, *symbols: str) -> None: ...
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
        *,
        exchange_factory: Callable[[], Any] | None = None,
    ) -> None:
        if exchange is None:
            raise ValueError("CcxtStreamFeed requires an exchange or use from_exchange(...)")
        self._ex = exchange
        self._warmup_feed = warmup_feed
        self._timeframe = timeframe
        self._handler: Callable[[Candle], None] | None = None
        self._symbol_handlers: dict[str, Callable[[Candle], None]] = {}
        self._last_ts: int | None = None
        self._last_ts_by_symbol: dict[str, int] = {}
        self._lock = threading.Lock()
        # Lifecycle is per symbol: one client is shared by every watch loop, so
        # it may only be closed once the last loop exits. A global flag would
        # let one unsubscribing symbol tear the connection down for the rest.
        self._exchange_factory = exchange_factory
        self._running: set[str] = set()
        self._stop_requested: set[str] = set()
        self._stop_all = False
        self._closed = False

    @classmethod
    def from_exchange(
        cls,
        exchange_id: str,
        api_key: str,
        api_secret: str,
        password: str | None = None,
        timeframe: str = "1m",
        *,
        market_type: str = "spot",
    ) -> "CcxtStreamFeed":
        if ccxtpro is None:
            raise RuntimeError("ccxt.pro is not installed")
        klass = getattr(ccxtpro, exchange_id)
        config: dict[str, Any] = {"apiKey": api_key, "secret": api_secret, "enableRateLimit": True}
        if password:
            config["password"] = password
        if market_type == "futures":
            # Stream the derivatives markets, matching CcxtVenue/CcxtCandleFeed.
            config["options"] = {"defaultType": "swap"}
        warmup_feed = CcxtCandleFeed.from_exchange(
            exchange_id, api_key, api_secret, password, market_type=market_type
        )
        return cls(exchange=klass(config), warmup_feed=warmup_feed, timeframe=timeframe)

    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        if self._warmup_feed is None:
            raise RuntimeError("CcxtStreamFeed has no warmup feed configured")
        return self._warmup_feed.warmup_candles(symbol, timeframe, limit)

    def on_bar(self, handler: Callable[[Candle], None]) -> None:
        self._handler = handler

    def on_bar_for(self, symbol: str, handler: Callable[[Candle], None]) -> None:
        self._symbol_handlers[symbol] = handler

    def _on_candles(self, candles: list, symbol: str | None = None) -> None:
        """Emit newly-closed candles, dropping the forming final row and dupes."""
        with self._lock:
            for row in candles[:-1]:  # last row is the still-forming candle
                ts = int(row[0])
                last_ts = self._last_ts_by_symbol.get(symbol) if symbol is not None else self._last_ts
                if last_ts is not None and ts <= last_ts:
                    continue
                if symbol is None:
                    self._last_ts = ts
                else:
                    self._last_ts_by_symbol[symbol] = ts
                handler = self._symbol_handlers.get(symbol) if symbol is not None else None
                handler = handler or self._handler
                if handler is not None:
                    handler(_ohlcv_to_candle(row))

    def _should_run(self, symbol: str) -> bool:
        """Whether ``symbol``'s watch loop should keep going."""
        return not self._stop_all and symbol not in self._stop_requested

    def _ensure_open(self) -> None:
        """Recreate the client if a previous full stop closed it.

        A closed ccxt client cannot be reused, so restarting a cached feed
        needs a fresh one. Without a factory the feed stays single-use.
        """
        if not self._closed:
            return
        if self._exchange_factory is None:
            raise RuntimeError(
                "CcxtStreamFeed was closed and has no exchange_factory to rebuild it; "
                "construct it with exchange_factory=... to support restarts"
            )
        self._ex = self._exchange_factory()
        self._closed = False

    async def _watch_loop(self, symbol: str) -> None:
        try:
            while self._should_run(symbol):
                candles = await self._ex.watch_ohlcv(symbol, self._timeframe)
                if candles:
                    self._on_candles(candles, symbol)
        finally:
            self._running.discard(symbol)
            self._stop_requested.discard(symbol)
            # The client is shared: only the last loop out closes it, and only
            # once. Otherwise one symbol ending kills every other symbol.
            if not self._running and not self._closed:
                self._closed = True
                await self._ex.close()

    async def run_async(self, *symbols: str) -> None:
        if not symbols:
            raise ValueError("CcxtStreamFeed.run_async requires a symbol")
        symbol = symbols[0]
        # Starting a symbol clears any stop left over from a previous run, so a
        # cached feed can be restarted after all its bots stopped.
        self._stop_requested.discard(symbol)
        self._stop_all = False
        self._ensure_open()
        self._running.add(symbol)
        await self._watch_loop(symbol)

    def run(self, *symbols: str) -> None:
        if not symbols:
            raise ValueError("CcxtStreamFeed.run requires a symbol")
        asyncio.run(self.run_async(*symbols))

    def stop_symbol(self, symbol: str) -> None:
        """Stop one symbol's watch loop, leaving the others running.

        Idempotent. The loop exits after its current message.
        """
        self._stop_requested.add(symbol)

    def stop(self) -> None:
        """Stop every symbol. Idempotent."""
        self._stop_all = True


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
