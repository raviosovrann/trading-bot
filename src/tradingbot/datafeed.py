"""Candle feed implementations used by bots for market data."""

from __future__ import annotations

from typing import Any, Protocol

from .models import Candle

try:
    import ccxt  # type: ignore
except Exception:  # pragma: no cover - optional third-party install
    ccxt = None  # type: ignore[assignment]


class CandleFeed(Protocol):
    """Protocol for candle providers used by the runtime."""

    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        """Return ``limit`` closed historical candles, oldest-first.

        Args:
            symbol: Trading symbol to fetch.
            timeframe: Candle granularity, e.g. ``1h``.
            limit: Maximum number of closed candles to return.

        Returns:
            Closed candles ordered oldest-first.
        """
        ...

    def latest_closed_candle(self, symbol: str, timeframe: str) -> Candle | None:
        """Return the most recently closed candle if available.

        Args:
            symbol: Trading symbol to fetch.
            timeframe: Candle granularity, e.g. ``1h``.

        Returns:
            The latest closed candle, or ``None`` when unavailable.
        """
        ...


def _ohlcv_to_candle(row: Any) -> Candle:
    """Convert a ccxt OHLCV row into a ``Candle`` model.

    ccxt OHLCV row shape: ``[timestamp_ms, open, high, low, close, volume]``.

    Args:
        row: OHLCV sequence returned by ccxt.

    Returns:
        A ``Candle`` populated from the row.
    """
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
        """Wrap an initialized ccxt exchange instance.

        Args:
            exchange: Initialized ccxt exchange. Must not be ``None``.

        Raises:
            ValueError: If ``exchange`` is ``None``.
        """
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
        *,
        market_type: str = "spot",
    ) -> "CcxtCandleFeed":
        """Create a feed from a ccxt exchange id and API credentials.

        Args:
            exchange_id: ccxt exchange class name, e.g. ``coinbase``.
            api_key: Exchange API key.
            api_secret: Exchange API secret.
            password: Optional exchange password.

        Returns:
            Configured ``CcxtCandleFeed`` instance.

        Raises:
            RuntimeError: If ccxt is not installed.
        """
        if ccxt is None:
            raise RuntimeError("ccxt is not installed")
        klass = getattr(ccxt, exchange_id)
        config: dict[str, Any] = {"apiKey": api_key, "secret": api_secret, "enableRateLimit": True}
        if password:
            config["password"] = password
        if market_type == "futures":
            # Fetch OHLCV from the derivatives markets, matching CcxtVenue.
            config["options"] = {"defaultType": "swap"}
        return cls(klass(config))

    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        """Fetch ``limit`` closed historical candles.

        Args:
            symbol: Trading symbol to fetch.
            timeframe: Candle granularity.
            limit: Number of closed candles to return.

        Returns:
            Closed candles ordered oldest-first.
        """
        if limit <= 0:
            return []
        rows = self._ex.fetch_ohlcv(symbol, timeframe, limit=limit + 1)
        if not rows:
            return []
        # Drop the still-forming final bar, keep the newest `limit` closed bars.
        closed = list(rows[:-1])
        return [_ohlcv_to_candle(r) for r in closed[-limit:]]

    def latest_closed_candle(self, symbol: str, timeframe: str) -> Candle | None:
        """Fetch the most recently closed candle.

        Args:
            symbol: Trading symbol to fetch.
            timeframe: Candle granularity.

        Returns:
            The latest closed candle, or ``None`` when unavailable.
        """
        rows = self._ex.fetch_ohlcv(symbol, timeframe, limit=2)
        if not rows:
            return None
        # rows[-1] is the forming bar; return the last closed one when present.
        closed_row = rows[-2] if len(rows) >= 2 else rows[-1]
        return _ohlcv_to_candle(closed_row)
