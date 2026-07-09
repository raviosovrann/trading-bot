from __future__ import annotations

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

    def process_candle(self, candle: Candle) -> OrderResult | None:
        if self._candles and candle.timestamp <= self._candles[-1].timestamp:
            return None

        self._candles.append(candle)
        if len(self._candles) > self._max_buffer:
            self._candles = self._candles[-self._max_buffer:]

        signal = self._strategy.on_bar(self._candles)
        if signal is None:
            return None

        return self._router.route(signal)

    def run_once(self) -> OrderResult | None:
        candle = self._feed.latest_closed_candle(self._symbol, self._timeframe)
        if candle is None:
            return None

        return self.process_candle(candle)
