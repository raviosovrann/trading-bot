from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from .models import Action, Candle, OrderType, PositionSide, Signal


class Strategy(Protocol):
    def on_bar(self, candles: Sequence[Candle]) -> Signal | None: ...


class SMACrossoverStrategy:
    def __init__(
        self,
        *,
        symbol: str,
        strategy_name: str = "sma_crossover",
        fast_length: int = 5,
        slow_length: int = 20,
        quantity: float = 0.001,
    ) -> None:
        if fast_length <= 0 or slow_length <= 0:
            raise ValueError("fast_length and slow_length must be > 0")
        if fast_length >= slow_length:
            raise ValueError("fast_length must be < slow_length")

        self.symbol = symbol
        self.strategy_name = strategy_name
        self.fast_length = fast_length
        self.slow_length = slow_length
        self.quantity = quantity

    @staticmethod
    def _sma(values: Sequence[float]) -> float:
        return sum(values) / len(values)

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        min_bars = self.slow_length + 1
        if len(candles) < min_bars:
            return None

        closes = [c.close for c in candles]

        prev_fast = self._sma(closes[-1 - self.fast_length: -1])
        prev_slow = self._sma(closes[-1 - self.slow_length: -1])
        curr_fast = self._sma(closes[-self.fast_length:])
        curr_slow = self._sma(closes[-self.slow_length:])

        crossed_up = prev_fast <= prev_slow and curr_fast > curr_slow
        if crossed_up:
            return Signal(
                strategy=self.strategy_name,
                action=Action.buy,
                symbol=self.symbol,
                order_type=OrderType.market,
                quantity=self.quantity,
                position_side=PositionSide.long,
            )

        crossed_down = prev_fast >= prev_slow and curr_fast < curr_slow
        if crossed_down:
            return Signal(
                strategy=self.strategy_name,
                action=Action.sell,
                symbol=self.symbol,
                order_type=OrderType.market,
                quantity=self.quantity,
                position_side=PositionSide.flat,
            )

        return None
