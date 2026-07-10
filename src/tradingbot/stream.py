from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from .datafeed import AlpacaCandleFeed, CoinbaseCandleFeed, _bar_to_candle
from .models import Candle

try:
    from alpaca.data.live import CryptoDataStream

    _ALPACA_STREAM_AVAILABLE = True
except Exception:  # pragma: no cover - optional third-party install
    CryptoDataStream = None  # type: ignore[assignment,misc]
    _ALPACA_STREAM_AVAILABLE = False

try:
    from coinbase.websocket import WSClient as _CoinbaseWSClient

    _COINBASE_WS_AVAILABLE = True
except Exception:  # pragma: no cover - optional third-party install
    _CoinbaseWSClient = None  # type: ignore[assignment,misc]
    _COINBASE_WS_AVAILABLE = False


class StreamingFeed(Protocol):
    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]: ...
    def on_bar(self, handler: Callable[[Candle], None]) -> None: ...
    def run(self, *symbols: str) -> None: ...
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
        if CryptoDataStream is None:
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


def _candle_from_coinbase_dict(c: dict) -> Candle:
    return Candle(
        timestamp=int(float(c.get("start", 0))) * 1000,
        open=float(c.get("open", 0.0)),
        high=float(c.get("high", 0.0)),
        low=float(c.get("low", 0.0)),
        close=float(c.get("close", 0.0)),
        volume=float(c.get("volume", 0.0)),
    )


class CoinbaseStreamFeed:
    """Event-driven push feed backed by Coinbase Advanced Trade's WebSocket.

    Mirrors ``AlpacaStreamFeed``: warmup history over REST (via
    ``CoinbaseCandleFeed``), live closed candles arriving on the ``candles``
    channel are normalized to ``Candle``, deduped by strictly-newer timestamp,
    and handed to a single registered handler.
    """

    def __init__(self, client: Any | None = None, warmup_feed: Any | None = None) -> None:
        if client is None:
            raise ValueError(
                "CoinbaseStreamFeed requires a client or use from_credentials(...)"
            )
        self._client = client
        self._warmup_feed = warmup_feed
        self._handler: Callable[[Candle], None] | None = None
        self._last_ts: int | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_credentials(
        cls, api_key: str, api_secret: str, sandbox: bool = True
    ) -> "CoinbaseStreamFeed":
        if _CoinbaseWSClient is None:
            raise RuntimeError("coinbase-advanced-py is not installed")
        warmup_feed = CoinbaseCandleFeed.from_credentials(api_key, api_secret, sandbox=sandbox)
        # The WS client needs the feed's message handler, so create the client
        # with a forwarding closure, build the feed, then wire them together.
        holder: dict[str, Any] = {}

        def _on_message(message: Any) -> None:
            holder["feed"]._on_ws_message(message)

        client = _CoinbaseWSClient(api_key=api_key, api_secret=api_secret, on_message=_on_message)
        feed = cls(client=client, warmup_feed=warmup_feed)
        holder["feed"] = feed
        return feed

    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        if self._warmup_feed is None:
            raise RuntimeError("CoinbaseStreamFeed has no warmup feed configured")
        return self._warmup_feed.warmup_candles(symbol, timeframe, limit)

    def on_bar(self, handler: Callable[[Candle], None]) -> None:
        self._handler = handler

    def _emit_candle(self, candle: Candle) -> None:
        with self._lock:
            if self._last_ts is not None and candle.timestamp <= self._last_ts:
                return
            self._last_ts = candle.timestamp
            handler = self._handler
            if handler is not None:
                handler(candle)

    def _on_ws_message(self, message: Any) -> None:
        data = json.loads(message) if isinstance(message, (str, bytes, bytearray)) else message
        if not isinstance(data, dict) or data.get("channel") != "candles":
            return
        for event in data.get("events", []):
            for raw in event.get("candles", []):
                self._emit_candle(_candle_from_coinbase_dict(raw))

    def run(self, *product_ids: str) -> None:
        self._client.open()
        self._client.subscribe(product_ids=list(product_ids), channels=["candles"])
        self._client.run_forever_with_exception_check()

    def stop(self) -> None:
        self._client.close()


def build_stream_feed(cfg: Any) -> StreamingFeed:
    """Select the streaming feed for the configured venue."""
    if cfg.venue == "alpaca":
        return AlpacaStreamFeed.from_credentials(cfg.alpaca_api_key, cfg.alpaca_api_secret)
    if cfg.venue == "coinbase":
        return CoinbaseStreamFeed.from_credentials(
            cfg.coinbase_api_key, cfg.coinbase_api_secret, sandbox=cfg.coinbase_sandbox
        )
    raise ValueError(f"Streaming not supported for venue: {cfg.venue}")


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
