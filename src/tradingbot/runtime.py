from __future__ import annotations

import signal
import time
from collections.abc import Callable

from .datafeed import CandleFeed
from .models import Candle, OrderResult
from .router import SignalRouter
from .strategy import Strategy
from .stream import StreamingFeed, run_with_reconnect


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


class StreamRuntime:
    """Event-driven runtime driven by a push-based ``StreamingFeed``.

    Warms up the candle buffer once over REST, registers ``process_candle`` as
    the feed's bar callback, then blocks on the WebSocket event loop. Replaces
    the retired ``run_forever()`` polling loop; the strategy, router and venues
    are unchanged.
    """

    def __init__(
        self,
        *,
        feed: StreamingFeed,
        strategy: Strategy,
        router: SignalRouter,
        symbol: str,
        timeframe: str,
        warmup_bars: int = 20,
        max_buffer: int = 500,
        base_backoff: float = 1.0,
        max_backoff: float = 60.0,
        gapfill_bars: int = 50,
    ) -> None:
        self._feed = feed
        self._symbol = symbol
        self._timeframe = timeframe
        self._stopped = False
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff
        self._gapfill_bars = gapfill_bars
        # Reuse BotRuntime for warmup + buffer + the pure process_candle core.
        self._bot = BotRuntime(
            feed=feed,
            strategy=strategy,
            router=router,
            symbol=symbol,
            timeframe=timeframe,
            warmup_bars=warmup_bars,
            max_buffer=max_buffer,
        )
        feed.on_bar(self._bot.process_candle)

    @property
    def candles(self) -> tuple[Candle, ...]:
        return self._bot.candles

    def start(self, *, install_signals: bool = True, sleep: Callable[[float], None] = time.sleep) -> None:
        if install_signals:
            self._install_signal_handlers()
        run_with_reconnect(
            connect_and_run=lambda: self._feed.run(self._symbol),
            should_stop=lambda: self._stopped,
            gap_fill=self._gap_fill,
            sleep=sleep,
            base_backoff=self._base_backoff,
            max_backoff=self._max_backoff,
        )

    def _gap_fill(self) -> None:
        """REST-fill bars missed during an outage; process_candle dedups overlap."""
        for candle in self._feed.warmup_candles(self._symbol, self._timeframe, self._gapfill_bars):
            self._bot.process_candle(candle)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._feed.stop()

    def _install_signal_handlers(self) -> None:
        def _handler(signum, frame):  # noqa: ANN001 - signal handler signature
            self.stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):  # pragma: no cover - non-main-thread guard
                pass
