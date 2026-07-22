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
    """The contract every market-data feed implements, native or ccxt-backed.

    Two responsibilities that are easy to conflate: ``warmup_candles`` is a
    *pull* over REST for history, while ``on_bar`` + ``run_async`` are the
    *push* path for live bars. A feed needs both because a strategy cannot
    evaluate until its buffer is warm, and warmth cannot come from a stream
    that only carries bars from the moment you subscribe.

    Implementations must emit each closed candle exactly once. Venues
    universally include the still-forming bar in their updates, so every
    implementation drops it and dedups on timestamp -- see ``_on_candles``.
    ``MarketDataHub`` and the per-symbol lifecycle in #112 depend on this, so
    a feed that double-emits will double-trade.
    """

    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        """Fetch closed historical candles to seed a strategy's buffer.

        Args:
            symbol: Venue symbol to fetch.
            timeframe: Candle interval, e.g. ``1m``.
            limit: Number of closed candles wanted, newest last.

        Returns:
            Closed candles oldest-first, excluding the forming bar.
        """
        ...

    def on_bar(self, handler: Callable[[Candle], None]) -> None:
        """Register the callback invoked as each candle closes.

        Args:
            handler: Called with one closed candle per bar. Runs on the
                stream's own thread or event loop, so it must not block.
        """
        ...

    async def run_async(self, *symbols: str) -> None:
        """Stream ``symbols`` until stopped, awaiting the venue's socket.

        Args:
            *symbols: Symbols to subscribe. Implementations may accept only
                the first; the hub drives one symbol per feed instance.
        """
        ...

    def run(self, *symbols: str) -> None:
        """Blocking equivalent of :meth:`run_async`, for non-async callers.

        Args:
            *symbols: Symbols to subscribe.
        """
        ...

    def stop(self) -> None:
        """Ask the stream to exit. Idempotent, and safe from another thread."""
        ...


class StreamingNotSupported(RuntimeError):
    """Raised when the ccxt client cannot stream candles for this venue.

    Distinct from a dropped connection: this is a missing capability, so
    retrying or restarting the bot can never help. Callers surface it as a
    start failure rather than letting a bot run without data (#170).

    Note this is usually a *client library* gap rather than a venue one.
    Coinbase's own WebSocket carries a ``candles`` channel and ccxt happily
    streams its trades and order book — ccxt simply has not implemented
    ``watch_ohlcv`` for it. See #171.
    """


def _require_ohlcv_streaming(exchange: Any) -> None:
    """Refuse an exchange client that cannot stream candles.

    The whole feed is built on ``watch_ohlcv``. Where ccxt has not implemented
    it (coinbase, for one) the warmup still succeeds over REST, so the bot
    starts, reports ``running`` and then never receives a bar. Failing here
    turns that silent dead end into an explicit start failure.

    Clients that do not advertise capabilities at all — test doubles, non-ccxt
    feeds — are left alone.

    Args:
        exchange: ccxt.pro-style client to check.

    Raises:
        StreamingNotSupported: If the client declares no ``watchOHLCV`` support.
    """
    capabilities = getattr(exchange, "has", None)
    if not isinstance(capabilities, dict) or "watchOHLCV" not in capabilities:
        return
    if capabilities.get("watchOHLCV"):
        return
    name = getattr(exchange, "id", None) or type(exchange).__name__
    raise StreamingNotSupported(
        f"ccxt does not implement watchOHLCV for {name}, so this bot cannot "
        "receive streaming candles. The venue's own WebSocket may well work — "
        "ccxt just has no OHLCV mapping for it — so this is a missing "
        "capability, not a connection problem, and restarting will not help. "
        "Use a venue ccxt streams candles for (binance, kraken), or see issue "
        "#171 for building candles from the trade stream."
    )


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
        """Wrap an existing ccxt.pro client.

        Args:
            exchange: ccxt.pro client. Required -- there is no default, because
                constructing one needs credentials this class should not hold.
            warmup_feed: Sync REST feed for history. Optional only so tests can
                exercise the streaming path alone; ``warmup_candles`` raises
                without it.
            timeframe: Candle interval passed to ``watch_ohlcv``.
            exchange_factory: Rebuilds the client after a full ``stop()``. A
                closed ccxt client cannot be reused, so without this the feed
                is single-use and a restarted bot gets a dead socket.

        Raises:
            ValueError: If ``exchange`` is None.
            StreamingNotSupported: If the client cannot stream candles.
        """
        if exchange is None:
            raise ValueError("CcxtStreamFeed requires an exchange or use from_exchange(...)")
        _require_ohlcv_streaming(exchange)
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
        """Build a feed and its REST warmup companion from credentials.

        Both halves are constructed here so they cannot disagree about which
        market they address: a spot warmup paired with a swap stream would
        silently seed the buffer from the wrong book.

        Args:
            exchange_id: ccxt exchange id, e.g. ``binance``.
            api_key: API key.
            api_secret: API secret.
            password: Passphrase, for venues that require one.
            timeframe: Candle interval to stream.
            market_type: ``spot`` or ``futures``; the latter selects the
                venue's derivatives markets.

        Returns:
            A feed with its warmup feed already wired.

        Raises:
            RuntimeError: If ccxt.pro is not installed.
            StreamingNotSupported: If the venue has no ``watch_ohlcv``.
        """
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
        """Fetch history over REST, delegating to the injected warmup feed.

        ccxt.pro's WebSocket carries no history, so this is a separate REST
        round trip rather than a replay of the stream.

        Args:
            symbol: Venue symbol to fetch.
            timeframe: Candle interval.
            limit: Number of closed candles wanted.

        Returns:
            Closed candles, oldest-first.

        Raises:
            RuntimeError: If no warmup feed was configured.
        """
        if self._warmup_feed is None:
            raise RuntimeError("CcxtStreamFeed has no warmup feed configured")
        return self._warmup_feed.warmup_candles(symbol, timeframe, limit)

    def on_bar(self, handler: Callable[[Candle], None]) -> None:
        """Register the fallback handler used for symbols without their own.

        Args:
            handler: Called with each closed candle.
        """
        self._handler = handler

    def on_bar_for(self, symbol: str, handler: Callable[[Candle], None]) -> None:
        """Register a handler for one symbol, overriding the fallback.

        One client is shared by every watch loop, so a hub serving several
        bots needs each symbol's bars to reach only that symbol's runtime.

        Args:
            symbol: Symbol this handler is responsible for.
            handler: Called with each closed candle for ``symbol``.
        """
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
        """Watch one symbol until it is stopped.

        Only the first symbol is used: the hub runs one loop per symbol so
        they can be stopped independently, and a single loop watching several
        would make ``stop_symbol`` impossible.

        Args:
            *symbols: Symbols to watch; only ``symbols[0]`` is subscribed.

        Raises:
            ValueError: If no symbol is given.
            RuntimeError: If the feed was closed and cannot be rebuilt.
        """
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
        """Blocking wrapper around :meth:`run_async` via ``asyncio.run``.

        For callers outside an event loop -- notably ``StreamRuntime.start``
        under ``run_with_reconnect``. Do not call from a running loop.

        Args:
            *symbols: Symbols to watch; only the first is subscribed.

        Raises:
            ValueError: If no symbol is given.
        """
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
