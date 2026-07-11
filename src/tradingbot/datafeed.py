from __future__ import annotations

from typing import Any, Protocol

from .models import Candle

try:
    import ccxt  # type: ignore
except Exception:  # pragma: no cover - optional third-party install
    ccxt = None  # type: ignore[assignment]


class CandleFeed(Protocol):
    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]: ...
    def latest_closed_candle(self, symbol: str, timeframe: str) -> Candle | None: ...


def _ohlcv_to_candle(row: Any) -> Candle:
    # ccxt OHLCV row shape: [timestamp_ms, open, high, low, close, volume]
    return Candle(
        timestamp=int(row[0]),
        open=float(row[1]),
        high=float(row[2]),
        low=float(row[3]),
        close=float(row[4]),
        volume=float(row[5]),
    )


class CcxtCandleFeed:
    """Pull-based candle feed backed by any ccxt exchange's ``fetch_ohlcv``.

    ccxt returns OHLCV rows oldest-first, and the final row is the currently
    *forming* bar. Both reads drop that forming bar so callers only ever see
    closed candles: ``warmup_candles`` fetches ``limit + 1`` and returns the
    newest ``limit`` closed bars; ``latest_closed_candle`` returns the
    second-to-last row when two are available.
    """

    def __init__(self, exchange: Any | None = None) -> None:
        if exchange is None:
            raise ValueError("CcxtCandleFeed requires an exchange or use from_exchange(...)")
        self._ex = exchange

    @classmethod
    def from_exchange(
        cls,
        exchange_id: str,
        api_key: str,
        api_secret: str,
        password: str | None = None,
    ) -> "CcxtCandleFeed":
        if ccxt is None:
            raise RuntimeError("ccxt is not installed")
        klass = getattr(ccxt, exchange_id)
        config: dict[str, Any] = {"apiKey": api_key, "secret": api_secret, "enableRateLimit": True}
        if password:
            config["password"] = password
        return cls(klass(config))

    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        if limit <= 0:
            return []
        rows = self._ex.fetch_ohlcv(symbol, timeframe, limit=limit + 1)
        if not rows:
            return []
        # Drop the still-forming final bar, keep the newest `limit` closed bars.
        closed = list(rows[:-1])
        return [_ohlcv_to_candle(r) for r in closed[-limit:]]

    def latest_closed_candle(self, symbol: str, timeframe: str) -> Candle | None:
        rows = self._ex.fetch_ohlcv(symbol, timeframe, limit=2)
        if not rows:
            return None
        # rows[-1] is the forming bar; return the last closed one when present.
        closed_row = rows[-2] if len(rows) >= 2 else rows[-1]
        return _ohlcv_to_candle(closed_row)
