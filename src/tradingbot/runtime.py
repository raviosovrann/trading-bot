from __future__ import annotations

import time
from collections.abc import Callable

from .datafeed import CandleFeed
from .models import Candle, OrderResult
from .router import SignalRouter
from .strategy import Strategy


class BotRuntime:
    def __init__(
        self,
        *,
        feed: CandleFeed,
        strategy: Strategy,
        router: SignalRouter,
        symbol: str,
        timeframe: str,
        warmup_bars: int = 0,
        max_buffer: int = 500,
    ) -> None:
        self._feed = feed
        self._strategy = strategy
        self._router = router
        self._symbol = symbol
        self._timeframe = timeframe
        self._max_buffer = max_buffer
        self._candles: list[Candle] = []

        if warmup_bars > 0:
            self._candles.extend(feed.warmup_candles(symbol, timeframe, warmup_bars))

    @property
    def candles(self) -> tuple[Candle, ...]:
        return tuple(self._candles)

    def run_once(self) -> OrderResult | None:
        candle = self._feed.latest_closed_candle(self._symbol, self._timeframe)
        if candle is None:
            return None

        if self._candles and candle.timestamp <= self._candles[-1].timestamp:
            return None

        self._candles.append(candle)
        if len(self._candles) > self._max_buffer:
            self._candles = self._candles[-self._max_buffer:]

        signal = self._strategy.on_bar(self._candles)
        if signal is None:
            return None

        return self._router.route(signal)

    def run_forever(
        self,
        *,
        sleep_seconds: float = 1.0,
        sleep_fn: Callable[[float], None] = time.sleep,
        max_iterations: int | None = None,
        swallow_exceptions: bool = True,
        on_exception: Callable[[Exception], None] | None = None,
    ) -> list[OrderResult]:
        results: list[OrderResult] = []
        iterations = 0

        while max_iterations is None or iterations < max_iterations:
            try:
                result = self.run_once()
                if result is not None:
                    results.append(result)
            except Exception as exc:
                if on_exception is not None:
                    on_exception(exc)
                if not swallow_exceptions:
                    raise

            iterations += 1
            sleep_fn(sleep_seconds)

        return results
