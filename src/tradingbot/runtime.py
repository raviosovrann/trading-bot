from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from .models import Candle, OrderResult
from .router import SignalRouter
from .strategies.base import Strategy
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
        """Configure the decision core.

        Args:
            strategy: Evaluated against the whole buffer on each new bar.
            router: Turns a signal into an order against the venue.
            max_buffer: Candles retained. Caps memory on a long-running bot,
                so it must exceed the longest lookback the strategy needs --
                a buffer trimmed below that silently stops producing signals.
            on_event: Receives JSON-encoded decision and order events. Its
                failures are logged and swallowed: an observer must never
                prevent an order from being processed.
            event_symbol: Symbol stamped on emitted events, for the supervisor
                to attribute them when several bots share an event stream.
        """
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
        """The current buffer, oldest-first.

        A tuple copy rather than the live list: callers inspecting state must
        not be able to mutate what the strategy will next be evaluated on.
        """
        return tuple(self._candles)

    def _trim(self) -> None:
        if len(self._candles) > self._max_buffer:
            self._candles = self._candles[-self._max_buffer:]

    def seed(self, candles: Iterable[Candle]) -> None:
        """Bulk-load warmup history into the buffer.

        Unlike ``add_candle`` this does not dedup or check ordering: it is the
        initial REST fill into an empty buffer, before any streaming bar has
        arrived. Gap-fill after an outage goes through ``process_candle``
        instead, precisely because that overlap does need deduping.

        Args:
            candles: Closed candles, oldest-first.
        """
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

        outcome = self._router.route_detailed(signal)
        result = outcome.result
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
            # The order as submitted and the result in full, so the supervisor
            # can record durable ledger events rather than reconstructing a
            # trade from the handful of fields above (#135). A close carries no
            # order, since it goes through close_position().
            "order": outcome.order.model_dump(mode="json") if outcome.order else None,
            "result": result.model_dump(mode="json"),
        })
        return result

    def process_candle(self, candle: Candle) -> OrderResult | None:
        """Buffer a candle and evaluate the strategy if it was genuinely new.

        The dedup guard is what makes gap-fill safe to overlap with the live
        stream: a bar already seen is dropped without re-running the strategy,
        so a reconnect that refetches recent history cannot re-trade it.

        Args:
            candle: A closed candle from the feed or a gap-fill.

        Returns:
            The routed order's result, or None if the candle was a duplicate
            or the strategy produced no signal.
        """
        # Streaming path: only evaluate on a genuinely new bar.
        if not self.add_candle(candle):
            return None
        return self.evaluate()


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
        """Wire the feed to a decision core and warm the buffer.

        Warmup happens here rather than in ``start`` so that construction
        either yields a runnable bot or fails outright -- a runtime that
        reported ``running`` on an empty buffer would look healthy while being
        unable to produce a signal.

        Args:
            feed: Push feed supplying live bars and REST history.
            strategy: Evaluated on each new bar.
            router: Routes signals to the venue.
            symbol: Symbol this runtime trades.
            timeframe: Candle interval.
            warmup_bars: History fetched before streaming. Must cover the
                strategy's lookback or it cannot signal until enough live bars
                accumulate. Non-positive skips warmup entirely.
            max_buffer: Candles retained by the processor.
            base_backoff: Initial reconnect delay, in seconds.
            max_backoff: Ceiling on the reconnect delay, in seconds.
            gapfill_bars: Bars refetched after an outage. Should exceed the
                bars a plausible outage spans, since anything older than this
                window is simply lost.
            on_event: Receives JSON-encoded decision and order events.
            run_blocking: Runs sync venue calls off the event loop. The service
                passes its per-bot worker lane so that work serializes with
                incoming bars (#111); standalone use gets a plain thread.
        """
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
        """The processor's current buffer, oldest-first."""
        return self._proc.candles

    def _on_candle(self, candle: Candle) -> None:
        # StreamingFeed.on_bar handlers return None; the order result is logged
        # elsewhere, not needed here.
        self._proc.process_candle(candle)

    def start(self, *, install_signals: bool = True, sleep: Callable[[float], None] = time.sleep) -> None:
        """Evaluate the warmed buffer, then stream until stopped.

        Blocks for the life of the bot. Use ``start_async`` under the service,
        which supervises many bots on one event loop.

        Args:
            install_signals: Install SIGINT/SIGTERM handlers. Only safe on the
                main thread of a process this runtime owns -- the service runs
                bots on worker threads and manages its own shutdown.
            sleep: Injected for tests, to avoid real reconnect delays.
        """
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
        """Stop streaming and close the feed. Idempotent.

        The guard matters: ``stop`` arrives from a signal handler, the
        supervisor, or both at once during shutdown, and the feed underneath
        is not guaranteed to tolerate a second close.

        In-flight work is not interrupted -- the loop exits after the message
        it is handling, so an order already being routed still completes.
        """
        if self._stopped:
            return
        self._stopped = True
        self._feed.stop()

    def _install_signal_handlers(self) -> None:
        def _handler(_signum: int, _frame: object) -> None:
            """Stop the runtime on SIGINT/SIGTERM.

            The signal number and frame are part of the handler contract but
            unused: any of the handled signals means the same thing here.
            """
            self.stop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):  # pragma: no cover - non-main-thread guard
                pass
