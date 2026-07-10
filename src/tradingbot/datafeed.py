from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from .models import Candle

try:
    from alpaca.data.historical import CryptoHistoricalDataClient
    from alpaca.data.requests import CryptoBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    _ALPACA_AVAILABLE = True
except Exception:  # pragma: no cover - optional third-party install
    CryptoHistoricalDataClient = None  # type: ignore[assignment,misc]
    CryptoBarsRequest = None  # type: ignore[assignment,misc]
    TimeFrame = None  # type: ignore[assignment,misc]
    TimeFrameUnit = None  # type: ignore[assignment,misc]
    _ALPACA_AVAILABLE = False

try:
    from coinbase.rest import RESTClient as _CoinbaseRESTClient

    _COINBASE_AVAILABLE = True
except Exception:  # pragma: no cover - optional third-party install
    _CoinbaseRESTClient = None  # type: ignore[assignment,misc]
    _COINBASE_AVAILABLE = False

_TF_RE = re.compile(r"^(\d+)(Min|Hour|Day)$")
_TF_UNIT_MAP = {"Min": "Minute", "Hour": "Hour", "Day": "Day"}


class CandleFeed(Protocol):
    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]: ...
    def latest_closed_candle(self, symbol: str, timeframe: str) -> Candle | None: ...


def normalize_candle(value: Candle | Mapping[str, float | int]) -> Candle:
    if isinstance(value, Candle):
        return value

    data = dict(value)
    return Candle(
        timestamp=int(data.get("timestamp", data.get("t", 0))),
        open=float(data.get("open", data.get("o", 0.0))),
        high=float(data.get("high", data.get("h", 0.0))),
        low=float(data.get("low", data.get("l", 0.0))),
        close=float(data.get("close", data.get("c", 0.0))),
        volume=float(data.get("volume", data.get("v", 0.0))),
    )


def _parse_timeframe(tf: str) -> Any:
    if TimeFrame is None or TimeFrameUnit is None:
        raise RuntimeError("alpaca-py is not installed")
    m = _TF_RE.match(tf)
    if not m:
        raise ValueError(f"Unsupported timeframe: {tf!r}. Expected e.g. '5Min', '1Hour', '1Day'.")
    amount = int(m.group(1))
    unit = getattr(TimeFrameUnit, _TF_UNIT_MAP[m.group(2)])
    return TimeFrame(amount, unit)


def _bar_to_candle(bar: Any) -> Candle:
    ts = getattr(bar, "timestamp", None)
    if ts is None:
        ts_ms = 0
    elif hasattr(ts, "timestamp"):
        ts_ms = int(ts.timestamp() * 1000)
    else:
        ts_ms = int(ts)
    return Candle(
        timestamp=ts_ms,
        open=float(getattr(bar, "open", 0.0)),
        high=float(getattr(bar, "high", 0.0)),
        low=float(getattr(bar, "low", 0.0)),
        close=float(getattr(bar, "close", 0.0)),
        volume=float(getattr(bar, "volume", 0.0)),
    )


class AlpacaCandleFeed:
    def __init__(self, client: Any | None = None) -> None:
        if client is None:
            raise ValueError("AlpacaCandleFeed requires a client or use from_credentials(...)")
        self._client = client

    @classmethod
    def from_credentials(cls, api_key: str, api_secret: str) -> "AlpacaCandleFeed":
        if CryptoHistoricalDataClient is None:
            raise RuntimeError("alpaca-py is not installed")
        return cls(CryptoHistoricalDataClient(api_key=api_key, secret_key=api_secret))

    def _fetch_bars(self, symbol: str, timeframe: str, limit: int) -> list[Any]:
        if CryptoBarsRequest is None:
            raise RuntimeError("alpaca-py is not installed")
        tf = _parse_timeframe(timeframe)
        request = CryptoBarsRequest(symbol_or_symbols=symbol, timeframe=tf, limit=limit)
        barset = self._client.get_crypto_bars(request)
        try:
            bars = barset[symbol]
        except (KeyError, TypeError):
            bars = []
        return list(bars) if bars is not None else []

    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        if limit <= 0:
            return []
        bars = self._fetch_bars(symbol, timeframe, limit + 1)
        return [_bar_to_candle(b) for b in bars[-limit:]]

    def latest_closed_candle(self, symbol: str, timeframe: str) -> Candle | None:
        bars = self._fetch_bars(symbol, timeframe, 2)
        if not bars:
            return None
        return _bar_to_candle(bars[-1])


_COINBASE_GRANULARITY_MAP: dict[str, str] = {
    "1Min": "ONE_MINUTE",
    "5Min": "FIVE_MINUTE",
    "15Min": "FIFTEEN_MINUTE",
    "30Min": "THIRTY_MINUTE",
    "1Hour": "ONE_HOUR",
    "2Hour": "TWO_HOUR",
    "6Hour": "SIX_HOUR",
    "1Day": "ONE_DAY",
}

_COINBASE_TF_SECONDS_MAP: dict[str, int] = {
    "1Min": 60,
    "5Min": 300,
    "15Min": 900,
    "30Min": 1800,
    "1Hour": 3600,
    "2Hour": 7200,
    "6Hour": 21600,
    "1Day": 86400,
}

_COINBASE_SANDBOX_BASE_URL = "api-sandbox.coinbase.com"


def _coinbase_granularity(tf: str) -> str:
    try:
        return _COINBASE_GRANULARITY_MAP[tf]
    except KeyError:
        raise ValueError(f"Unsupported timeframe for Coinbase: {tf!r}. Expected one of {list(_COINBASE_GRANULARITY_MAP)}")


def _timeframe_seconds(tf: str) -> int:
    try:
        return _COINBASE_TF_SECONDS_MAP[tf]
    except KeyError:
        raise ValueError(f"Unsupported timeframe: {tf!r}. Expected one of {list(_COINBASE_TF_SECONDS_MAP)}")


def _coinbase_candle_to_candle(bar: Any) -> Candle:
    return Candle(
        timestamp=int(float(getattr(bar, "start", 0))) * 1000,
        open=float(getattr(bar, "open", 0.0)),
        high=float(getattr(bar, "high", 0.0)),
        low=float(getattr(bar, "low", 0.0)),
        close=float(getattr(bar, "close", 0.0)),
        volume=float(getattr(bar, "volume", 0.0)),
    )


class CoinbaseCandleFeed:
    def __init__(self, client: Any | None = None) -> None:
        if client is None:
            raise ValueError("CoinbaseCandleFeed requires a client or use from_credentials(...)")
        self._client = client

    @classmethod
    def from_credentials(cls, api_key: str, api_secret: str, sandbox: bool = True) -> "CoinbaseCandleFeed":
        if _CoinbaseRESTClient is None:
            raise RuntimeError("coinbase-advanced-py is not installed")
        if sandbox:
            return cls(
                client=_CoinbaseRESTClient(
                    api_key=api_key,
                    api_secret=api_secret,
                    base_url=_COINBASE_SANDBOX_BASE_URL,
                )
            )
        return cls(client=_CoinbaseRESTClient(api_key=api_key, api_secret=api_secret))

    def _fetch_candles(self, symbol: str, timeframe: str, limit: int) -> list[Any]:
        from datetime import datetime, timezone

        product_id = symbol.replace("/", "-")
        granularity = _coinbase_granularity(timeframe)
        tf_secs = _timeframe_seconds(timeframe)
        now = int(datetime.now(timezone.utc).timestamp())
        start = now - (limit + 1) * tf_secs
        response = self._client.get_candles(product_id, start=start, end=now, granularity=granularity)
        raw = getattr(response, "candles", None)
        if not raw:
            return []
        return sorted(raw, key=lambda c: int(getattr(c, "start", 0)))

    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        if limit <= 0:
            return []
        bars = self._fetch_candles(symbol, timeframe, limit + 1)
        return [_coinbase_candle_to_candle(b) for b in bars[-limit:]]

    def latest_closed_candle(self, symbol: str, timeframe: str) -> Candle | None:
        bars = self._fetch_candles(symbol, timeframe, 2)
        if not bars:
            return None
        return _coinbase_candle_to_candle(bars[-1])


def build_feed(cfg: Any) -> CandleFeed:
    if cfg.venue == "alpaca":
        return AlpacaCandleFeed.from_credentials(cfg.alpaca_api_key, cfg.alpaca_api_secret)
    if cfg.venue == "fake":
        return InMemoryCandleFeed()
    if cfg.venue == "coinbase":
        return CoinbaseCandleFeed.from_credentials(
            cfg.coinbase_api_key, cfg.coinbase_api_secret, sandbox=cfg.coinbase_sandbox
        )
    raise ValueError(f"Unsupported venue: {cfg.venue}")


class InMemoryCandleFeed:
    """Simple in-memory candle feed with per-symbol sequential reads."""

    def __init__(
        self,
        candles_by_symbol: Mapping[str, Sequence[Candle | Mapping[str, float | int]]] | None = None,
    ) -> None:
        self._candles: dict[str, list[Candle]] = {}
        self._cursor: dict[str, int] = {}

        if candles_by_symbol:
            for symbol, candles in candles_by_symbol.items():
                self._candles[symbol] = [normalize_candle(c) for c in candles]
                self._cursor[symbol] = 0

    def append(self, symbol: str, candle: Candle | Mapping[str, float | int]) -> None:
        self._candles.setdefault(symbol, []).append(normalize_candle(candle))
        self._cursor.setdefault(symbol, 0)

    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        del timeframe
        if limit <= 0:
            return []

        candles = self._candles.get(symbol, [])
        start = self._cursor.get(symbol, 0)
        end = min(start + limit, len(candles))
        self._cursor[symbol] = end
        return list(candles[start:end])

    def latest_closed_candle(self, symbol: str, timeframe: str) -> Candle | None:
        del timeframe
        candles = self._candles.get(symbol, [])
        idx = self._cursor.get(symbol, 0)
        if idx >= len(candles):
            return None
        candle = candles[idx]
        self._cursor[symbol] = idx + 1
        return candle
