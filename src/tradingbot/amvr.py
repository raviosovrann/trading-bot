"""Adaptive Momentum Velocity Ribbon (AMVR) strategy.

A long-only spot strategy ported from the ChartPrime "Adaptive Momentum
Velocity Ribbon" TradingView indicator. It builds three Hull moving averages
(the ribbon), measures each one's velocity (slope) and acceleration, and enters
a long only when a bullish "prepare" signal has armed the setup AND all three
ribbons are rising AND base momentum is accelerating up AND both the 1H and 4H
higher timeframes are accelerating up. It exits (flattens to cash) on the
bearish prepare signal. Being spot, it never shorts.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from typing import Any

from .models import Action, Candle, OrderType, PositionSide, Signal


# --------------------------------------------------------------------------- #
# HMA engine (faithful port of the Pine hma() / velocity / acceleration math)
# --------------------------------------------------------------------------- #

def _wma(window: Sequence[float]) -> float:
    """Weighted moving average over ``window`` (oldest→newest, newest weighted
    highest), matching Pine's ``ta.wma``."""
    n = len(window)
    denom = n * (n + 1) / 2.0
    return sum(v * (i + 1) for i, v in enumerate(window)) / denom


def _wma_series(values: Sequence[float], length: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(values)):
        if length <= 0 or i + 1 < length:
            out.append(None)
        else:
            out.append(_wma(values[i + 1 - length: i + 1]))
    return out


def _hma_series(closes: Sequence[float], length: int) -> list[float | None]:
    """Hull MA series: ``WMA(2·WMA(close, len/2) − WMA(close, len), √len)``."""
    half = max(1, round(length / 2))
    sqrt_len = max(1, round(math.sqrt(length)))

    wma_half = _wma_series(closes, half)
    wma_full = _wma_series(closes, length)

    raw: list[float | None] = []
    for a, b in zip(wma_half, wma_full):
        raw.append(None if a is None or b is None else 2.0 * a - b)

    out: list[float | None] = []
    for i in range(len(raw)):
        if i + 1 < sqrt_len:
            out.append(None)
            continue
        window = raw[i + 1 - sqrt_len: i + 1]
        if any(x is None for x in window):
            out.append(None)
        else:
            out.append(_wma([x for x in window if x is not None]))
    return out


def _velocity(hma: Sequence[float | None], lookback: int, idx: int) -> float | None:
    if idx < 0 or idx - lookback < 0:
        return None
    cur, prev = hma[idx], hma[idx - lookback]
    if cur is None or prev is None:
        return None
    return cur - prev


def _velocity_series(hma: Sequence[float | None], lookback: int) -> list[float | None]:
    return [_velocity(hma, lookback, i) for i in range(len(hma))]


def _accelerating_up(hma: Sequence[float | None], lookback: int, idx: int) -> bool:
    """True when velocity at ``idx`` is positive and greater than the prior bar
    (i.e. the dashboard "▲ Acceleration" state)."""
    vel = _velocity(hma, lookback, idx)
    vel_prev = _velocity(hma, lookback, idx - 1)
    if vel is None or vel_prev is None:
        return False
    return vel > 0 and (vel - vel_prev) > 0


# --------------------------------------------------------------------------- #
# strategy
# --------------------------------------------------------------------------- #

class AdaptiveMomentumRibbonStrategy:
    def __init__(
        self,
        *,
        symbol: str,
        mtf_feed: Any,
        strategy_name: str = "amvr",
        fast_len: int = 40,
        mid_len: int = 80,
        slow_len: int = 100,
        mtf_len: int = 20,
        lookback: int = 2,
        threshold: float = 0.0,
        arming_max_bars: int = 200,
        quantity: float = 0.001,
        htf1: str = "1h",
        htf2: str = "4h",
        mtf_bars: int = 40,
        mtf_cache_seconds: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not (fast_len < mid_len < slow_len):
            raise ValueError("require fast_len < mid_len < slow_len")
        self.symbol = symbol
        self._mtf_feed = mtf_feed
        self.strategy_name = strategy_name
        self.fast_len = fast_len
        self.mid_len = mid_len
        self.slow_len = slow_len
        self.mtf_len = mtf_len
        self.lookback = lookback
        self.threshold = threshold
        self.arming_max_bars = arming_max_bars
        self.quantity = quantity
        self.htf1 = htf1
        self.htf2 = htf2
        self.mtf_bars = mtf_bars
        self.mtf_cache_seconds = mtf_cache_seconds
        self._clock = clock

        self._in_position = False
        self._mtf_cache: dict[str, tuple[float, list[float]]] = {}

    # -- higher-timeframe momentum -------------------------------------------

    def _mtf_closes(self, timeframe: str) -> list[float]:
        now = self._clock()
        cached = self._mtf_cache.get(timeframe)
        if cached is not None and (now - cached[0]) < self.mtf_cache_seconds:
            return cached[1]
        candles = self._mtf_feed.warmup_candles(self.symbol, timeframe, self.mtf_bars)
        closes = [c.close for c in candles]
        self._mtf_cache[timeframe] = (now, closes)
        return closes

    def _htf_accelerating(self, timeframe: str) -> bool:
        closes = self._mtf_closes(timeframe)
        hma = _hma_series(closes, self.mtf_len)
        return _accelerating_up(hma, self.lookback, len(hma) - 1)

    # -- prepare-signal arming (derived from the buffer) ---------------------

    def _bull_armed(self, vel1: Sequence[float | None], last: int) -> bool:
        """A bullish prepare (vel1 crosses above threshold) is the most recent
        crossover and happened within ``arming_max_bars`` bars."""
        last_bull = last_bear = None
        for i in range(1, len(vel1)):
            prev, cur = vel1[i - 1], vel1[i]
            if prev is None or cur is None:
                continue
            if prev <= self.threshold < cur:
                last_bull = i
            elif prev >= self.threshold > cur:
                last_bear = i
        if last_bull is None:
            return False
        if last_bear is not None and last_bear >= last_bull:
            return False
        return (last - last_bull) <= self.arming_max_bars

    def _bear_cross(self, vel1: Sequence[float | None], last: int) -> bool:
        prev, cur = vel1[last - 1], vel1[last]
        if prev is None or cur is None:
            return False
        return prev >= self.threshold > cur

    # -- main hook -----------------------------------------------------------

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        closes = [c.close for c in candles]
        hma1 = _hma_series(closes, self.fast_len)
        hma2 = _hma_series(closes, self.mid_len)
        hma3 = _hma_series(closes, self.slow_len)

        last = len(closes) - 1
        vel1 = _velocity(hma1, self.lookback, last)
        vel2 = _velocity(hma2, self.lookback, last)
        vel3 = _velocity(hma3, self.lookback, last)
        if vel1 is None or vel2 is None or vel3 is None:
            return None

        vel1_series = _velocity_series(hma1, self.lookback)

        # Exit first: bearish prepare flattens an open long (base timeframe only).
        if self._in_position:
            if self._bear_cross(vel1_series, last):
                self._in_position = False
                return Signal(
                    strategy=self.strategy_name,
                    action=Action.close,
                    symbol=self.symbol,
                    order_type=OrderType.market,
                    quantity=self.quantity,
                    position_side=PositionSide.flat,
                )
            return None

        # Entry: all base conditions, then confirm on the higher timeframes.
        all_ribbons_green = vel1 > 0 and vel2 > 0 and vel3 > 0
        base_accelerating = _accelerating_up(hma1, self.lookback, last)
        if not (self._bull_armed(vel1_series, last) and all_ribbons_green and base_accelerating):
            return None

        if not (self._htf_accelerating(self.htf1) and self._htf_accelerating(self.htf2)):
            return None

        self._in_position = True
        return Signal(
            strategy=self.strategy_name,
            action=Action.buy,
            symbol=self.symbol,
            order_type=OrderType.market,
            quantity=self.quantity,
            position_side=PositionSide.long,
        )
