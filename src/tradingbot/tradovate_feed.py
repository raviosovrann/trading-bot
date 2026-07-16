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
        if client is None:
            raise ValueError("TradovateCandleFeed requires a client or use from_credentials(...)")
        self._client = client

    @classmethod
    def from_credentials(cls, md_access_token: str) -> "TradovateCandleFeed":
        if websockets is None:
            raise RuntimeError("websockets is not installed")
        return cls(_TradovateMdClient(md_access_token))

    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        if limit <= 0:
            return []
        rows = self._client.get_chart(symbol, timeframe, limit + 1)
        if not rows:
            return []
        # Drop the still-forming final bar, keep the newest `limit` closed bars.
        closed = list(rows[:-1])
        return [_ohlcv_to_candle(r) for r in closed[-limit:]]

    def latest_closed_candle(self, symbol: str, timeframe: str) -> Candle | None:
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
        if websockets is None:
            raise RuntimeError("websockets is not installed")
        client = _TradovateMdClient(md_access_token)
        return cls(client=client, warmup_feed=TradovateCandleFeed(client), timeframe=timeframe)

    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        if self._warmup_feed is None:
            raise RuntimeError("TradovateStreamFeed has no warmup feed configured")
        return self._warmup_feed.warmup_candles(symbol, timeframe, limit)

    def on_bar(self, handler: Callable[[Candle], None]) -> None:
        self._handler = handler

    def on_bar_for(self, symbol: str, handler: Callable[[Candle], None]) -> None:
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
        if not symbols:
            raise ValueError("TradovateStreamFeed.run_async requires a symbol")
        await self._watch_loop(symbols[0])

    def run(self, *symbols: str) -> None:
        if not symbols:
            raise ValueError("TradovateStreamFeed.run requires a symbol")
        asyncio.run(self.run_async(*symbols))

    def stop(self) -> None:
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
        self._token = md_access_token
        self._url = url

    def get_chart(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:  # pragma: no cover - network
        raise NotImplementedError(
            "Tradovate MD get_chart not wired yet — implement the md/getChart WebSocket "
            "request against the demo env (see api.tradovate.com)."
        )

    async def watch_chart(self, symbol: str, timeframe: str) -> list[list[float]]:  # pragma: no cover - network
        raise NotImplementedError(
            "Tradovate MD watch_chart not wired yet — implement the chart subscription "
            "over wss://md.tradovateapi.com against the demo env."
        )

    async def close(self) -> None:  # pragma: no cover - network
        return None
