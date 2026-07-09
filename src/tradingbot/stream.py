from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Protocol

from .datafeed import AlpacaCandleFeed, _bar_to_candle
from .models import Candle

try:
    from alpaca.data.live import CryptoDataStream

    _ALPACA_STREAM_AVAILABLE = True
except Exception:  # pragma: no cover - optional third-party install
    CryptoDataStream = None  # type: ignore[assignment,misc]
    _ALPACA_STREAM_AVAILABLE = False


class StreamingFeed(Protocol):
    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]: ...
    def on_bar(self, handler: Callable[[Candle], None]) -> None: ...
    def run(self) -> None: ...
    def stop(self) -> None: ...


class AlpacaStreamFeed:
    """Event-driven push feed backed by Alpaca's CryptoDataStream.

    Warmup history is fetched over REST (via an injected ``AlpacaCandleFeed``),
    while live closed bars arrive over the websocket and are normalized to
    ``Candle`` before being handed to a single registered handler. Bars whose
    timestamp is not strictly newer than the last emitted one are dropped so a
    given candle is never emitted twice.
    """

    def __init__(self, client: Any | None = None, warmup_feed: Any | None = None) -> None:
        if client is None:
            raise ValueError(
                "AlpacaStreamFeed requires a client or use from_credentials(...)"
            )
        self._client = client
        self._warmup_feed = warmup_feed
        self._handler: Callable[[Candle], None] | None = None
        self._last_ts: int | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_credentials(cls, api_key: str, api_secret: str) -> "AlpacaStreamFeed":
        if not _ALPACA_STREAM_AVAILABLE:
            raise RuntimeError("alpaca-py is not installed")
        client = CryptoDataStream(api_key, api_secret)
        warmup_feed = AlpacaCandleFeed.from_credentials(api_key, api_secret)
        return cls(client=client, warmup_feed=warmup_feed)

    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        if self._warmup_feed is None:
            raise RuntimeError("AlpacaStreamFeed has no warmup feed configured")
        return self._warmup_feed.warmup_candles(symbol, timeframe, limit)

    def on_bar(self, handler: Callable[[Candle], None]) -> None:
        self._handler = handler

    def _on_ws_bar(self, bar: Any) -> None:
        candle = _bar_to_candle(bar)
        with self._lock:
            if self._last_ts is not None and candle.timestamp <= self._last_ts:
                return
            self._last_ts = candle.timestamp
            handler = self._handler
            if handler is not None:
                handler(candle)

    async def _async_on_ws_bar(self, bar: Any) -> None:
        """Async adapter so the sync ``_on_ws_bar`` can be used as an Alpaca handler."""
        self._on_ws_bar(bar)

    def run(self, *symbols: str) -> None:
        self._client.subscribe_bars(self._async_on_ws_bar, *symbols)
        self._client.run()

    def stop(self) -> None:
        self._client.stop()
