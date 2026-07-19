"""Native Coinbase Advanced Trade market data — no ccxt involved.

ccxt has no ``watch_ohlcv`` implementation for coinbase (#170), and Coinbase's
own ``candles`` WebSocket channel is fixed at five-minute buckets, so neither
can serve the 1m bots this project runs. Both problems disappear if candles are
aggregated from the ``market_trades`` channel, which streams every fill and can
therefore be bucketed to any interval.

Two feeds live here:

* :class:`CoinbaseCandleFeed` — historical warmup over the public REST
  ``/market/products/{id}/candles`` endpoint.
* :class:`CoinbaseStreamFeed` — live candles aggregated from ``market_trades``,
  with ``heartbeats`` and per-message ``sequence_num`` used to notice a stream
  that is silently dropping messages.

Neither requires credentials: Coinbase serves both market-data surfaces
unauthenticated, which is what makes a credential-free demo path possible.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .models import Candle

_log = logging.getLogger(__name__)

WS_URL = "wss://advanced-trade-ws.coinbase.com"
"""Coinbase Advanced Trade market-data WebSocket. Public: no auth required."""

REST_URL = "https://api.coinbase.com/api/v3/brokerage/market/products"
"""Public market-data REST base. Also unauthenticated."""

_TIMEFRAMES: dict[str, tuple[str, int]] = {
    "1m": ("ONE_MINUTE", 60),
    "5m": ("FIVE_MINUTE", 300),
    "15m": ("FIFTEEN_MINUTE", 900),
    "30m": ("THIRTY_MINUTE", 1_800),
    "1h": ("ONE_HOUR", 3_600),
    "2h": ("TWO_HOUR", 7_200),
    "6h": ("SIX_HOUR", 21_600),
    "1d": ("ONE_DAY", 86_400),
}
"""House timeframe -> (Coinbase REST granularity, interval seconds)."""


def to_product_id(symbol: str) -> str:
    """Convert a house symbol to a Coinbase product id.

    Args:
        symbol: Symbol such as ``BTC/USD`` (or an already-converted ``BTC-USD``).

    Returns:
        Coinbase product id, e.g. ``BTC-USD``.
    """
    return symbol.strip().upper().replace("/", "-")


def to_symbol(product_id: str) -> str:
    """Convert a Coinbase product id back to a house symbol.

    Args:
        product_id: Product id such as ``BTC-USD``.

    Returns:
        House symbol, e.g. ``BTC/USD``.
    """
    return product_id.strip().upper().replace("-", "/")


def granularity(timeframe: str) -> str:
    """Return Coinbase's REST granularity name for ``timeframe``.

    Args:
        timeframe: House timeframe such as ``1m``.

    Returns:
        Granularity name, e.g. ``ONE_MINUTE``.

    Raises:
        ValueError: If the timeframe is not supported by Coinbase.
    """
    return _lookup(timeframe)[0]


def bucket_seconds(timeframe: str) -> int:
    """Return the interval length in seconds for ``timeframe``.

    Args:
        timeframe: House timeframe such as ``1m``.

    Returns:
        Interval length in seconds.

    Raises:
        ValueError: If the timeframe is not supported by Coinbase.
    """
    return _lookup(timeframe)[1]


def _lookup(timeframe: str) -> tuple[str, int]:
    """Return the (granularity, seconds) pair for ``timeframe``.

    Args:
        timeframe: House timeframe.

    Returns:
        Granularity name and interval length.

    Raises:
        ValueError: If the timeframe is not supported by Coinbase.
    """
    key = timeframe.strip().lower()
    if key not in _TIMEFRAMES:
        supported = ", ".join(sorted(_TIMEFRAMES))
        raise ValueError(f"unsupported Coinbase timeframe {timeframe!r}; supported: {supported}")
    return _TIMEFRAMES[key]


def _epoch_seconds(value: Any) -> float | None:
    """Parse a Coinbase trade timestamp into epoch seconds.

    The wire format is RFC 3339, but numeric values are accepted so tests can
    use plain offsets.

    Args:
        value: Timestamp string or number.

    Returns:
        Epoch seconds, or ``None`` when unparseable.
    """
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


class _Bucket:
    """One interval's running OHLCV, ordered by trade time rather than arrival."""

    __slots__ = ("start", "open", "high", "low", "close", "volume", "_first_ts", "_last_ts")

    def __init__(self, start: int, price: float, size: float, ts: float) -> None:
        """Open a bucket from its first observed trade.

        Args:
            start: Interval start, epoch seconds.
            price: Trade price.
            size: Trade size.
            ts: Trade time, epoch seconds.
        """
        self.start = start
        self.open = self.high = self.low = self.close = price
        self.volume = size
        self._first_ts = ts
        self._last_ts = ts

    def add(self, price: float, size: float, ts: float) -> None:
        """Fold another trade into this interval.

        Coinbase sends its snapshot newest-first, so arrival order is not
        chronological — open and close therefore track the earliest and latest
        trade *times*, not the first and last seen.

        Args:
            price: Trade price.
            size: Trade size.
            ts: Trade time, epoch seconds.
        """
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.volume += size
        if ts < self._first_ts:
            self._first_ts, self.open = ts, price
        if ts >= self._last_ts:
            self._last_ts, self.close = ts, price

    def to_candle(self) -> Candle:
        """Return this interval as a closed :class:`Candle`."""
        return Candle(
            timestamp=self.start * 1_000,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )


class TradeAggregator:
    """Bucket ``market_trades`` into closed candles for one product.

    The contract matches the ccxt feed it replaces: a candle is emitted
    **exactly once, when its interval has closed**, and the interval still in
    progress is never handed out. Intervals with no trades produce no candle —
    synthesizing flat bars would fabricate market activity that never happened,
    which on an illiquid pair could badly mislead a strategy.
    """

    def __init__(self, bucket: int) -> None:
        """Initialize the aggregator.

        Args:
            bucket: Interval length in seconds.

        Raises:
            ValueError: If ``bucket`` is not positive.
        """
        if bucket <= 0:
            raise ValueError("bucket must be positive")
        self._bucket = bucket
        self._open: dict[int, _Bucket] = {}
        self._seen: set[str] = set()
        self._closed_through: int | None = None
        self._lock = threading.Lock()

    def add(self, trade: Any) -> None:
        """Fold one raw trade into its interval, ignoring anything unusable.

        A malformed row is dropped rather than raised: one bad message must not
        break the stream for every bot on the symbol.

        Args:
            trade: Raw ``market_trades`` entry.
        """
        if not isinstance(trade, dict):
            return
        ts = _epoch_seconds(trade.get("time"))
        if ts is None:
            return
        try:
            price = float(trade["price"])
            size = float(trade.get("size", 0.0))
        except (KeyError, TypeError, ValueError):
            return

        trade_id = str(trade.get("trade_id", ""))
        start = int(ts // self._bucket) * self._bucket
        with self._lock:
            # A trade for an interval already published must not resurrect it:
            # re-emitting a bar the strategy acted on would let it trade the
            # same interval twice.
            if self._closed_through is not None and start <= self._closed_through:
                return
            if trade_id:
                if trade_id in self._seen:
                    return
                self._seen.add(trade_id)
            bucket = self._open.get(start)
            if bucket is None:
                self._open[start] = _Bucket(start, price, size, ts)
            else:
                bucket.add(price, size, ts)

    def close_elapsed(self, now: float) -> list[Candle]:
        """Return every interval that has closed since the last call.

        Args:
            now: Current time, epoch seconds.

        Returns:
            Newly closed candles, oldest first.
        """
        with self._lock:
            ready = sorted(start for start in self._open if start + self._bucket <= now)
            candles = [self._open.pop(start).to_candle() for start in ready]
            if ready:
                self._closed_through = max(ready)
            # Trade ids only guard against snapshot/update overlap, which is
            # bounded in time; drop them with their interval so the set cannot
            # grow without limit on a long-lived stream.
            if len(self._seen) > 10_000:
                self._seen.clear()
        return candles


class CoinbaseCandleFeed:
    """Historical candles over Coinbase's public market-data REST endpoint.

    Implements the :class:`~tradingbot.datafeed.CandleFeed` protocol. No
    credentials: the ``/market/products`` surface is unauthenticated, which is
    what lets a bot warm up without any key configured.
    """

    def __init__(self, http: Any | None = None, *, timeout: float = 20.0) -> None:
        """Initialize the feed.

        Args:
            http: Object with a ``get(url, params=, timeout=)`` method. Injected
                so tests never touch the network; defaults to ``httpx``.
            timeout: Per-request timeout in seconds.
        """
        if http is None:
            import httpx  # imported lazily so the module stays importable without it

            http = httpx
        self._http = http
        self._timeout = timeout

    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        """Return up to ``limit`` closed candles, oldest first.

        Args:
            symbol: House symbol, e.g. ``BTC/USD``.
            timeframe: House timeframe, e.g. ``1m``.
            limit: Maximum candles to return.

        Returns:
            Closed candles ordered oldest-first.

        Raises:
            ValueError: If the timeframe is not supported by Coinbase.
        """
        step = bucket_seconds(timeframe)
        product = to_product_id(symbol)
        end = int(time.time())
        # One extra interval of slack so a just-closed bar is included.
        start = end - step * (max(limit, 1) + 2)
        response = self._http.get(
            f"{REST_URL}/{product}/candles",
            params={
                "start": str(start),
                "end": str(end),
                "granularity": granularity(timeframe),
            },
            timeout=self._timeout,
        )
        response.raise_for_status()
        rows = response.json().get("candles", [])
        candles = [candle for candle in (self._to_candle(row) for row in rows) if candle]
        # Coinbase returns newest-first; the runtime dedups on increasing
        # timestamps, so the wrong order would drop every bar but the first.
        candles.sort(key=lambda candle: candle.timestamp)
        return candles[-limit:] if limit > 0 else []

    def latest_closed_candle(self, symbol: str, timeframe: str) -> Candle | None:
        """Return the most recently closed candle, or ``None`` when unavailable.

        Args:
            symbol: House symbol.
            timeframe: House timeframe.

        Returns:
            The newest closed candle, or ``None``.
        """
        candles = self.warmup_candles(symbol, timeframe, 2)
        return candles[-1] if candles else None

    def _to_candle(self, row: Any) -> Candle | None:
        """Convert one REST row into a :class:`Candle`, or ``None`` if unusable.

        Args:
            row: Raw candle mapping from the API.

        Returns:
            The parsed candle, or ``None`` when the row is malformed.
        """
        try:
            return Candle(
                timestamp=int(row["start"]) * 1_000,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume", 0.0)),
            )
        except (KeyError, TypeError, ValueError):
            _log.warning("skipping malformed Coinbase candle row: %r", row)
            return None


class CoinbaseStreamFeed:
    """Live candles aggregated from Coinbase's ``market_trades`` channel.

    Implements the :class:`~tradingbot.stream.StreamingFeed` protocol, including
    the per-symbol lifecycle #112 requires: each symbol runs its own connection
    and can be stopped without disturbing the others.

    Candles come from trades rather than Coinbase's ``candles`` channel because
    that channel is fixed at five-minute buckets and cannot serve a 1m bot.
    ``heartbeats`` is subscribed alongside to keep the connection alive, and
    every message's ``sequence_num`` is checked so a stream that is silently
    dropping messages is noticed rather than mistaken for a quiet market.
    """

    def __init__(
        self,
        *,
        timeframe: str = "1m",
        warmup_feed: Any | None = None,
        connect: Callable[[str], Any] | None = None,
        clock: Callable[[], float] = time.time,
        tick_seconds: float = 1.0,
        url: str = WS_URL,
    ) -> None:
        """Initialize the feed.

        Args:
            timeframe: Candle interval to aggregate to.
            warmup_feed: Feed used for historical candles; defaults to a
                :class:`CoinbaseCandleFeed`.
            connect: Async callable returning a connected WebSocket. Injected so
                tests never touch the network.
            clock: Time source, injected for deterministic tests.
            tick_seconds: How often closed intervals are flushed. Bars are
                closed on the clock, not on the next trade, so a quiet market
                still produces timely candles.
            url: WebSocket endpoint.

        Raises:
            ValueError: If the timeframe is not supported by Coinbase.
        """
        self._bucket = bucket_seconds(timeframe)
        self._timeframe = timeframe
        self._warmup_feed = warmup_feed if warmup_feed is not None else CoinbaseCandleFeed()
        self._connect = connect
        self._clock = clock
        self._tick = tick_seconds
        self._url = url
        self._handler: Callable[[Candle], None] | None = None
        self._symbol_handlers: dict[str, Callable[[Candle], None]] = {}
        self._gap_handlers: list[Callable[[str], None]] = []
        self._aggregators: dict[str, TradeAggregator] = {}
        self._sockets: dict[str, Any] = {}
        self._stop_requested: set[str] = set()
        self._stop_all = False

    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        """Delegate historical candles to the warmup feed.

        Args:
            symbol: House symbol.
            timeframe: House timeframe.
            limit: Maximum candles to return.

        Returns:
            Closed candles ordered oldest-first.
        """
        return self._warmup_feed.warmup_candles(symbol, timeframe, limit)

    def on_bar(self, handler: Callable[[Candle], None]) -> None:
        """Register a handler for every symbol.

        Args:
            handler: Callback invoked with each closed candle.
        """
        self._handler = handler

    def on_bar_for(self, symbol: str, handler: Callable[[Candle], None]) -> None:
        """Register a handler for one symbol.

        Args:
            symbol: House symbol.
            handler: Callback invoked with each closed candle.
        """
        self._symbol_handlers[to_symbol(to_product_id(symbol))] = handler

    def on_gap(self, handler: Callable[[str], None]) -> None:
        """Register a handler for detected message gaps.

        Args:
            handler: Callback invoked with a human-readable description.
        """
        self._gap_handlers.append(handler)

    async def run_async(self, *symbols: str) -> None:
        """Stream one symbol until stopped.

        Args:
            symbols: Trading symbols; the first is used.

        Raises:
            ValueError: If no symbol is given.
            RuntimeError: If no WebSocket connector is available.
        """
        if not symbols:
            raise ValueError("CoinbaseStreamFeed.run_async requires a symbol")
        symbol = to_symbol(to_product_id(symbols[0]))
        product = to_product_id(symbol)
        self._stop_requested.discard(symbol)
        self._stop_all = False
        self._aggregators.setdefault(symbol, TradeAggregator(self._bucket))

        socket = await self._open(self._url)
        self._sockets[symbol] = socket
        try:
            for channel in ("market_trades", "heartbeats"):
                await socket.send(json.dumps({
                    "type": "subscribe", "product_ids": [product], "channel": channel,
                }))
            await self._consume(symbol, socket)
        finally:
            self._sockets.pop(symbol, None)
            try:
                await socket.close()
            except Exception:  # noqa: BLE001 - closing must not mask the exit reason
                _log.debug("error closing Coinbase socket for %s", symbol, exc_info=True)

    async def _open(self, url: str) -> Any:
        """Open a WebSocket, using the injected connector when present.

        Args:
            url: Endpoint to connect to.

        Returns:
            A connected WebSocket.

        Raises:
            RuntimeError: If no connector is available.
        """
        if self._connect is not None:
            return await self._connect(url)
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise RuntimeError("CoinbaseStreamFeed requires the websockets package") from exc
        return await websockets.connect(url, open_timeout=20)

    async def _consume(self, symbol: str, socket: Any) -> None:
        """Read frames while flushing closed intervals, until the symbol stops.

        Reading and flushing are separate tasks on purpose. Bars close on the
        clock rather than on the next trade, so a quiet market still produces
        timely candles; and the reader is never interrupted mid-``recv``, which
        keeps the socket's own framing state out of the equation.

        Args:
            symbol: House symbol being streamed.
            socket: Connected WebSocket.
        """
        reader = asyncio.create_task(self._read_loop(symbol, socket))
        try:
            while self._should_run(symbol) and not reader.done():
                await asyncio.sleep(self._tick)
                self._flush(symbol)
        finally:
            if not reader.done():
                reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            self._flush(symbol)

    async def _read_loop(self, symbol: str, socket: Any) -> None:
        """Consume frames until cancelled or the socket ends.

        Args:
            symbol: House symbol being streamed.
            socket: Connected WebSocket.
        """
        last_sequence: int | None = None
        while True:
            raw = await socket.recv()
            message = self._parse(raw)
            if message is None:
                continue
            last_sequence = self._check_sequence(symbol, message, last_sequence)
            if message.get("channel") == "market_trades":
                self._ingest(symbol, message)

    def _parse(self, raw: Any) -> dict | None:
        """Decode one frame, returning ``None`` when it is unusable.

        Args:
            raw: Raw frame payload.

        Returns:
            The decoded message, or ``None``.
        """
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            _log.warning("skipping unparseable Coinbase frame")
            return None
        return message if isinstance(message, dict) else None

    def _check_sequence(self, symbol: str, message: dict, last: int | None) -> int | None:
        """Report a skipped ``sequence_num`` and return the new high-water mark.

        A gap means the socket is alive but messages went missing — trades we
        will never see, and therefore candles that are quietly wrong. The ccxt
        feed had no way to detect this.

        Args:
            symbol: House symbol being streamed.
            message: Decoded message.
            last: Previously seen sequence number.

        Returns:
            The sequence number to compare against next.
        """
        raw_sequence = message.get("sequence_num")
        if not isinstance(raw_sequence, int):
            return last
        if last is not None and raw_sequence > last + 1:
            missed = raw_sequence - last - 1
            detail = (
                f"{symbol}: missed {missed} Coinbase message(s) "
                f"(sequence {last} -> {raw_sequence})"
            )
            _log.warning("%s", detail)
            for handler in tuple(self._gap_handlers):
                try:
                    handler(detail)
                except Exception:  # noqa: BLE001 - a bad handler must not kill the stream
                    _log.exception("gap handler failed for %s", symbol)
        return raw_sequence

    def _ingest(self, symbol: str, message: dict) -> None:
        """Fold a ``market_trades`` message into the symbol's aggregator.

        Args:
            symbol: House symbol being streamed.
            message: Decoded message.
        """
        aggregator = self._aggregators.get(symbol)
        if aggregator is None:
            return
        for event in message.get("events", []):
            if not isinstance(event, dict):
                continue
            for trade in event.get("trades", []):
                aggregator.add(trade)

    def _flush(self, symbol: str) -> None:
        """Emit every interval that has closed for ``symbol``.

        Args:
            symbol: House symbol being streamed.
        """
        aggregator = self._aggregators.get(symbol)
        if aggregator is None:
            return
        for candle in aggregator.close_elapsed(self._clock()):
            handler = self._symbol_handlers.get(symbol) or self._handler
            if handler is None:
                continue
            try:
                handler(candle)
            except Exception:  # noqa: BLE001 - one bad handler must not stop the feed
                _log.exception("candle handler failed for %s", symbol)

    def _should_run(self, symbol: str) -> bool:
        """Return whether ``symbol``'s loop should continue.

        Args:
            symbol: House symbol.

        Returns:
            ``True`` while the symbol has not been stopped.
        """
        return not self._stop_all and symbol not in self._stop_requested

    def stop_symbol(self, symbol: str) -> None:
        """Stop one symbol, leaving the others streaming.

        Args:
            symbol: House symbol to stop.
        """
        self._stop_requested.add(to_symbol(to_product_id(symbol)))

    def stop(self) -> None:
        """Stop every symbol and release the connections."""
        self._stop_all = True
