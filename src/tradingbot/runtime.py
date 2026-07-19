from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from .datafeed import CandleFeed
from .models import Candle, OrderResult
from .router import SignalRouter
from .strategy import Strategy
from .stream import StreamingFeed, run_with_reconnect

_log = logging.getLogger(__name__)


class CandleProcessor:
    """Feed-agnostic candle buffer + strategy/router dispatch (no I/O).

    Both runtimes share this: a rolling buffer, timestamp dedup, and the pure
    ``process_candle`` decision core. It knows nothing about how candles arrive.
    """

    def __init__(
        self,
        *,
        strategy: Strategy,
        router: SignalRouter,
        max_buffer: int = 500,
        on_event: Callable[[str], None] | None = None,
        event_symbol: str | None = None,
    ) -> None:
        self._strategy = strategy
        self._router = router
        self._max_buffer = max_buffer
        self._on_event = on_event
        self._event_symbol = event_symbol
        self._candles: list[Candle] = []

    def _emit(self, event: dict[str, object]) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(json.dumps(event, separators=(",", ":")))
        except Exception:  # observer failures must not stop order processing
            _log.exception("candle processor observer failed")

    @property
    def candles(self) -> tuple[Candle, ...]:
        return tuple(self._candles)

    def _trim(self) -> None:
        if len(self._candles) > self._max_buffer:
            self._candles = self._candles[-self._max_buffer:]

    def seed(self, candles: Iterable[Candle]) -> None:
        self._candles.extend(candles)
        self._trim()

    def add_candle(self, candle: Candle) -> bool:
        """Append a candle if it is strictly newer than the last (dedup).
        Returns True if it was added."""
        if self._candles and candle.timestamp <= self._candles[-1].timestamp:
            return False
        self._candles.append(candle)
        self._trim()
        return True

    def evaluate(self) -> OrderResult | None:
        """Run the strategy on the current buffer and route any signal."""
        if not self._candles:
            return None

        signal = self._strategy.on_bar(self._candles)
        if signal is None:
            self._emit({
                "type": "decision",
                "symbol": self._event_symbol,
                "ts": self._candles[-1].timestamp,
                "text": "no signal",
            })
            return None

        result = self._router.route(signal)
        if result.status == "dry_run":
            label = "DRY-RUN (not sent)"
        elif result.ok:
            label = "PLACED"
        else:
            label = "FAILED"
        _log.info(
            "order %s: action=%s status=%s id=%s%s",
            label, signal.action.value, result.status, result.order_id,
            f" error={result.error}" if result.error else "",
        )
        self._emit({
            "type": "order",
            "action": signal.action.value,
            "status": result.status,
            "ok": result.ok,
            "order_id": result.order_id,
            "symbol": signal.symbol,
            "ts": self._candles[-1].timestamp,
        })
        return result

    def process_candle(self, candle: Candle) -> OrderResult | None:
        # Streaming path: only evaluate on a genuinely new bar.
        if not self.add_candle(candle):
            return None
        return self.evaluate()


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
        self._symbol = symbol
        self._timeframe = timeframe
        self._proc = CandleProcessor(
            strategy=strategy,
            router=router,
            max_buffer=max_buffer,
            event_symbol=symbol,
        )

        if warmup_bars > 0:
            self._proc.seed(feed.warmup_candles(symbol, timeframe, warmup_bars))

    @property
    def candles(self) -> tuple[Candle, ...]:
        return self._proc.candles

    def process_candle(self, candle: Candle) -> OrderResult | None:
        return self._proc.process_candle(candle)

    def run_once(self) -> OrderResult | None:
        # Warmup already loaded history up to the latest closed candle, so the
        # freshly-fetched latest is usually a duplicate. Add it only if newer,
        # then evaluate the current buffer regardless — otherwise one-shot mode
        # would dedup the candle away and never run the strategy.
        candle = self._feed.latest_closed_candle(self._symbol, self._timeframe)
        if candle is not None:
            self._proc.add_candle(candle)
        elif not self._proc.candles:
            _log.info("%s: no candles available (empty warmup and no latest)", self._symbol)
            return None
        return self._proc.evaluate()


class StreamRuntime:
    """Event-driven runtime driven by a push-based ``StreamingFeed``.

    Warms up the candle buffer once over REST, registers ``process_candle`` as
    the feed's bar callback, then blocks on the WebSocket event loop (with
    reconnection). Replaces the retired ``run_forever()`` polling loop; the
    strategy, router and venues are unchanged.
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
        on_event: Callable[[str], None] | None = None,
        run_blocking: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        # Strategy evaluation and gap-fill both reach synchronous venue calls.
        # The service hands in its per-bot worker lane so that work is
        # serialized with incoming bars and kept off the event loop (#111);
        # standalone use falls back to a plain worker thread.
        self._run_blocking: Callable[..., Awaitable[Any]] = (
            run_blocking if run_blocking is not None else asyncio.to_thread
        )
        self._feed = feed
        self._symbol = symbol
        self._timeframe = timeframe
        self._stopped = False
        self._base_backoff = base_backoff
        self._max_backoff = max_backoff
        self._gapfill_bars = gapfill_bars
        self._proc = CandleProcessor(
            strategy=strategy,
            router=router,
            max_buffer=max_buffer,
            on_event=on_event,
            event_symbol=symbol,
        )

        if warmup_bars > 0:
            self._proc.seed(feed.warmup_candles(symbol, timeframe, warmup_bars))
        feed.on_bar(self._on_candle)

    @property
    def candles(self) -> tuple[Candle, ...]:
        return self._proc.candles

    def _on_candle(self, candle: Candle) -> None:
        # StreamingFeed.on_bar handlers return None; the order result is logged
        # elsewhere, not needed here.
        self._proc.process_candle(candle)

    def start(self, *, install_signals: bool = True, sleep: Callable[[float], None] = time.sleep) -> None:
        if install_signals:
            self._install_signal_handlers()
        # Evaluate the warmed buffer immediately so we act on an already-valid
        # signal and give instant feedback, rather than waiting up to a full
        # candle (e.g. an hour on 1h) for the first decision.
        _log.info(
            "%s: warmed up %d bars — evaluating now, then streaming live candles",
            self._symbol, len(self._proc.candles),
        )
        self._proc.evaluate()
        run_with_reconnect(
            connect_and_run=lambda: self._feed.run(self._symbol),
            should_stop=lambda: self._stopped,
            gap_fill=self._gap_fill,
            sleep=sleep,
            base_backoff=self._base_backoff,
            max_backoff=self._max_backoff,
        )

    async def start_async(
        self,
        *,
        install_signals: bool = False,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Start async streaming with reconnect and gap-fill supervision."""
        if install_signals:
            self._install_signal_handlers()
        _log.info(
            "%s: warmed up %d bars — evaluating now, then streaming async candles",
            self._symbol, len(self._proc.candles),
        )
        await self._run_blocking(self._proc.evaluate)
        backoff = self._base_backoff
        while not self._stopped:
            try:
                await self._feed.run_async(self._symbol)
                healthy = True
            except asyncio.CancelledError:
                raise
            except Exception:
                healthy = False
                _log.exception("%s: async stream disconnected", self._symbol)
            if self._stopped:
                break
            if healthy:
                backoff = self._base_backoff
            await sleep(backoff)
            if self._stopped:
                break
            await self._run_blocking(self._gap_fill)
            if not healthy:
                backoff = min(backoff * 2, self._max_backoff)

    def _gap_fill(self) -> None:
        """REST-fill bars missed during an outage; process_candle dedups overlap."""
        for candle in self._feed.warmup_candles(self._symbol, self._timeframe, self._gapfill_bars):
            self._proc.process_candle(candle)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._feed.stop()

    def _install_signal_handlers(self) -> None:
        def _handler(signum: int, frame: object) -> None:
            self.stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):  # pragma: no cover - non-main-thread guard
                pass
