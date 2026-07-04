from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from .models import Candle


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
