"""Tradovate market-data feeds (candles + streaming) for CME crypto futures.

Mirrors the ccxt feeds: all logic maps onto an injected market-data client, so
the feeds are fully unit-testable with a fake and no network. The real client
(``_TradovateMdClient``) talks to Tradovate's Market Data WebSocket
(``wss://md.tradovateapi.com/v1/websocket``) and is built in ``from_credentials``.

NOTE: the real WebSocket client's frame protocol and chart fields MUST be
verified against https://api.tradovate.com/ on the demo environment before live
use. Bars are normalized to the ccxt-style ``[ts_ms, o, h, l, c, v]`` row so the
same ``_ohlcv_to_candle`` conversion and dedup logic apply.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

from .datafeed import _ohlcv_to_candle
from .models import Candle

try:
    import websockets  # type: ignore
except Exception:  # pragma: no cover - optional third-party install
    websockets = None  # type: ignore[assignment]

_MD_WS_URL = "wss://md.tradovateapi.com/v1/websocket"


class TradovateCandleFeed:
    """REST/chart candle feed backed by an injected Tradovate MD client."""

    def __init__(self, client: Any | None = None) -> None:
        """Wrap an injected market-data client.

        Args:
            client: MD client exposing ``get_chart``. Required -- the feed
                never builds its own, which is what keeps it testable with a
                fake and free of network access.

        Raises:
            ValueError: If ``client`` is None.
        """
        if client is None:
            raise ValueError("TradovateCandleFeed requires a client or use from_credentials(...)")
        self._client = client

    @classmethod
    def from_credentials(cls, md_access_token: str) -> "TradovateCandleFeed":
        """Build a feed on a real WebSocket client.

        Args:
            md_access_token: Tradovate market-data token, which is distinct
                from the trading access token used by ``TradovateVenue``.

        Returns:
            A feed backed by a live MD client.

        Raises:
            RuntimeError: If ``websockets`` is not installed.
        """
        if websockets is None:
            raise RuntimeError("websockets is not installed")
        return cls(_TradovateMdClient(md_access_token))

    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        """Fetch the newest ``limit`` closed bars for a strategy's buffer.

        Requests one extra bar because Tradovate's chart response ends with
        the bar still forming. Returning it would hand the strategy a candle
        whose close moves under it, and then a second, different candle with
        the same timestamp once it settles.

        Args:
            symbol: Tradovate contract symbol, e.g. ``MBTF6``.
            timeframe: Chart interval, e.g. ``1m``.
            limit: Number of closed bars wanted. Non-positive returns empty.

        Returns:
            Up to ``limit`` closed candles, oldest-first.
        """
        if limit <= 0:
            return []
        rows = self._client.get_chart(symbol, timeframe, limit + 1)
        if not rows:
            return []
        # Drop the still-forming final bar, keep the newest `limit` closed bars.
        closed = list(rows[:-1])
        return [_ohlcv_to_candle(r) for r in closed[-limit:]]

    def latest_closed_candle(self, symbol: str, timeframe: str) -> Candle | None:
        """Fetch the most recent settled bar.

        Takes the second-to-last row, the last being the forming bar. A
        single-row response is treated as already closed: that only happens
        when the venue returns no forming bar at all.

        Args:
            symbol: Tradovate contract symbol.
            timeframe: Chart interval.

        Returns:
            The latest closed candle, or None if the venue returned nothing.
        """
        rows = self._client.get_chart(symbol, timeframe, 2)
        if not rows:
            return None
        return _ohlcv_to_candle(rows[-2] if len(rows) >= 2 else rows[-1])


class TradovateStreamFeed:
    """Streaming feed over the Tradovate MD WebSocket (injected async client).

    Mirrors ``CcxtStreamFeed``: each ``watch_chart`` update returns recent bars
    with the final bar still forming; only strictly-newer closed bars are emitted,
    so a candle is handed to the handler exactly once, when it closes.
    """

    def __init__(self, client: Any | None = None, warmup_feed: Any | None = None, timeframe: str = "1m") -> None:
        """Wrap an injected async market-data client.

        Args:
            client: MD client exposing ``watch_chart`` and ``close``.
            warmup_feed: Candle feed for history. Optional so the streaming
                path can be tested alone; ``warmup_candles`` raises without it.
            timeframe: Chart interval to subscribe.

        Raises:
            ValueError: If ``client`` is None.
        """
        if client is None:
            raise ValueError("TradovateStreamFeed requires a client or use from_credentials(...)")
        self._client = client
        self._warmup_feed = warmup_feed
        self._timeframe = timeframe
        self._handler: Callable[[Candle], None] | None = None
        self._symbol_handlers: dict[str, Callable[[Candle], None]] = {}
        self._last_ts_by_symbol: dict[str, int] = {}
        self._stopped = False
        self._lock = threading.Lock()

    @classmethod
    def from_credentials(cls, md_access_token: str, timeframe: str = "1m") -> "TradovateStreamFeed":
        """Build a stream and its warmup feed sharing one MD client.

        The client is deliberately shared rather than duplicated: Tradovate
        counts concurrent market-data connections, and history plus live bars
        for one bot should not cost two of them.

        Args:
            md_access_token: Tradovate market-data token.
            timeframe: Chart interval to subscribe.

        Returns:
            A stream feed with its warmup feed already wired.

        Raises:
            RuntimeError: If ``websockets`` is not installed.
        """
        if websockets is None:
            raise RuntimeError("websockets is not installed")
        client = _TradovateMdClient(md_access_token)
        return cls(client=client, warmup_feed=TradovateCandleFeed(client), timeframe=timeframe)

    @property
    def warmup_feed(self) -> Any:
        """The candle feed sharing this stream's MD client (for hub wiring)."""
        return self._warmup_feed

    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        """Fetch history, delegating to the shared candle feed.

        Args:
            symbol: Tradovate contract symbol.
            timeframe: Chart interval.
            limit: Number of closed bars wanted.

        Returns:
            Closed candles, oldest-first.

        Raises:
            RuntimeError: If no warmup feed was configured.
        """
        if self._warmup_feed is None:
            raise RuntimeError("TradovateStreamFeed has no warmup feed configured")
        return self._warmup_feed.warmup_candles(symbol, timeframe, limit)

    def on_bar(self, handler: Callable[[Candle], None]) -> None:
        """Register the fallback handler for symbols without their own.

        Args:
            handler: Called with each closed candle.
        """
        self._handler = handler

    def on_bar_for(self, symbol: str, handler: Callable[[Candle], None]) -> None:
        """Register a handler for one symbol, overriding the fallback.

        Args:
            symbol: Symbol this handler is responsible for.
            handler: Called with each closed candle for ``symbol``.
        """
        self._symbol_handlers[symbol] = handler

    def _on_bars(self, rows: list, symbol: str) -> None:
        with self._lock:
            for row in rows[:-1]:  # last row is the still-forming bar
                ts = int(row[0])
                last_ts = self._last_ts_by_symbol.get(symbol)
                if last_ts is not None and ts <= last_ts:
                    continue
                self._last_ts_by_symbol[symbol] = ts
                handler = self._symbol_handlers.get(symbol) or self._handler
                if handler is not None:
                    handler(_ohlcv_to_candle(row))

    async def _watch_loop(self, symbol: str) -> None:
        try:
            while not self._stopped:
                rows = await self._client.watch_chart(symbol, self._timeframe)
                if rows:
                    self._on_bars(rows, symbol)
        finally:
            await self._client.close()

    async def run_async(self, *symbols: str) -> None:
        """Watch one symbol until stopped, closing the client on exit.

        Only the first symbol is subscribed, matching ``CcxtStreamFeed``: the
        hub runs one loop per symbol so each can be stopped independently.

        Args:
            *symbols: Symbols to watch; only ``symbols[0]`` is subscribed.

        Raises:
            ValueError: If no symbol is given.
        """
        if not symbols:
            raise ValueError("TradovateStreamFeed.run_async requires a symbol")
        await self._watch_loop(symbols[0])

    def run(self, *symbols: str) -> None:
        """Blocking wrapper around :meth:`run_async` via ``asyncio.run``.

        Args:
            *symbols: Symbols to watch; only the first is subscribed.

        Raises:
            ValueError: If no symbol is given.
        """
        if not symbols:
            raise ValueError("TradovateStreamFeed.run requires a symbol")
        asyncio.run(self.run_async(*symbols))

    def stop(self) -> None:
        """Ask the watch loop to exit after its current message.

        One-way: the flag is never cleared and the loop closes the MD client
        on the way out, so a stopped feed cannot be restarted. Unlike
        ``CcxtStreamFeed`` there is no ``exchange_factory`` equivalent here --
        the hub builds a fresh feed instead.
        """
        self._stopped = True


class _TradovateMdClient:
    """Real Tradovate Market Data client (WebSocket).

    VERIFY against https://api.tradovate.com/ on demo before live use. Tradovate
    MD uses a text-frame protocol over ``wss://md.tradovateapi.com/v1/websocket``:
    authorize with the ``mdAccessToken``, then ``md/getChart`` for history and
    chart subscriptions for live bars. Bars are normalized to
    ``[ts_ms, open, high, low, close, volume]``. This skeleton isolates that
    protocol so the feed classes above stay fully tested; the operator wires the
    exact frames during demo validation.
    """

    def __init__(self, md_access_token: str, *, url: str = _MD_WS_URL) -> None:
        """Record the connection details; no socket is opened here.

        Args:
            md_access_token: Tradovate market-data token.
            url: MD WebSocket endpoint. Overridable so demo validation can
                point at a capture or an alternate environment.
        """
        self._token = md_access_token
        self._url = url

    def get_chart(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:  # pragma: no cover - network
        """Fetch chart history over ``md/getChart``. **Not yet implemented.**

        Args:
            symbol: Tradovate contract symbol.
            timeframe: Chart interval.
            limit: Number of bars to request, forming bar included.

        Returns:
            Rows normalized to ``[ts_ms, open, high, low, close, volume]``, so
            the ccxt ``_ohlcv_to_candle`` conversion applies unchanged.

        Raises:
            NotImplementedError: Always, until the frames are verified on demo.
        """
        raise NotImplementedError(
            "Tradovate MD get_chart not wired yet — implement the md/getChart WebSocket "
            "request against the demo env (see api.tradovate.com)."
        )

    async def watch_chart(self, symbol: str, timeframe: str) -> list[list[float]]:  # pragma: no cover - network
        """Await the next chart update. **Not yet implemented.**

        Each update is expected to carry recent bars with the final one still
        forming, matching ``watch_ohlcv``; ``_on_bars`` relies on that shape.

        Args:
            symbol: Tradovate contract symbol.
            timeframe: Chart interval.

        Returns:
            Rows normalized to ``[ts_ms, open, high, low, close, volume]``.

        Raises:
            NotImplementedError: Always, until the frames are verified on demo.
        """
        raise NotImplementedError(
            "Tradovate MD watch_chart not wired yet — implement the chart subscription "
            "over wss://md.tradovateapi.com against the demo env."
        )

    async def close(self) -> None:  # pragma: no cover - network
        """Release the socket. A no-op until the client is wired.

        Deliberately does not raise, unlike the two methods above: the watch
        loop closes in a ``finally``, and a teardown that raised would mask
        whatever error actually ended the loop.
        """
        return None
