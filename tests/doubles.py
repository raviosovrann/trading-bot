"""Test-only doubles.

These stand in for real venues/feeds so the suite runs without network or
credentials. They are intentionally NOT part of the shipped ``tradingbot``
package — the application only ever talks to a real exchange via ccxt.
"""

from collections.abc import Mapping, Sequence

from tradingbot.models import (
    Candle,
    Order,
    OrderResult,
    OrderType,
    Position,
    PositionSide,
    Side,
)

_FLAT_TOL = 1e-9


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


class FakeVenue:
    """In-memory venue for tests: one net position per symbol."""

    def __init__(self) -> None:
        self.orders: list[Order] = []
        self._net: dict[str, float] = {}

    def place_order(self, order: Order) -> OrderResult:
        self.orders.append(order)
        delta = order.qty if order.side is Side.buy else -order.qty
        self._net[order.symbol] = self._net.get(order.symbol, 0.0) + delta
        return OrderResult(ok=True, order_id=str(len(self.orders)), status="filled",
                           filled_qty=order.qty, raw={})

    def get_position(self, symbol: str) -> Position | None:
        if symbol not in self._net:
            return None
        net = self._net[symbol]
        if abs(net) < _FLAT_TOL:
            side = PositionSide.flat
        elif net > 0:
            side = PositionSide.long
        else:
            side = PositionSide.short
        return Position(symbol=symbol, side=side, size=abs(net), entry_price=0.0)

    def close_position(self, symbol: str) -> OrderResult:
        pos = self.get_position(symbol)
        if pos is None or pos.side is PositionSide.flat:
            return OrderResult(ok=True, order_id=None, status="no position",
                               filled_qty=0.0, raw={})
        close_side = Side.sell if pos.side is PositionSide.long else Side.buy
        return self.place_order(Order(symbol=symbol, side=close_side,
                                      order_type=OrderType.market, qty=pos.size,
                                      reduce_only=True))

    def health_check(self) -> bool:
        return True
