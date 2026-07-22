"""Bot supervisor that manages bot lifecycle, wiring and event publishing."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from collections.abc import Iterable
from typing import Any, Callable, cast

from ..models import Candle, OrderResult, Position, PositionSide
from ..router import SignalRouter
from ..venues.contracts import ContractMetadataError, ContractSpec, spot_spec
from ..runtime import StreamRuntime
from ..strategies import StrategyContext
from ..stream import StreamingFeed
from .blocking import BlockingCalls, BlockingCallTimeout, WorkerPools
from .events import BotStateEvent, DecisionEvent, EventBus, OrderEvent
from .ledger import OrderLedger, events_from_payload, events_from_status
from .registry import build_strategy, build_venue
from .risk import GlobalExposure

_log = logging.getLogger(__name__)

_WARMUP_BARS = 220
"""Default number of warm-up bars requested when starting a bot."""

_STATE_POLL_SECONDS = 5.0
"""Default cadence for re-marking a running bot's position and PnL."""

_DERIVATIVE_MARKETS = frozenset({"futures", "swap", "perpetual", "perp", "option"})
"""Market types whose contract size must come from the venue, never a default."""


def _is_derivative_market(market_type: str) -> bool:
    """Return whether ``market_type`` names a derivative market."""
    return market_type.strip().lower() in _DERIVATIVE_MARKETS


def _quote_currency(symbol: str) -> str:
    """Best-effort quote currency from a ``BASE/QUOTE`` symbol.

    Only used for spot, where the quote currency is informational -- notional
    is ``qty x price`` regardless. A derivative never reaches this: its spec
    comes from the venue or the bot does not start.
    """
    _, _, quote = symbol.partition("/")
    return (quote.split(":")[0] or "USD").strip() or "USD"


@dataclass
class BotConfig:
    """Static configuration for one trading bot."""

    id: str
    """Unique bot identifier."""

    venue: str
    """Execution venue identifier."""

    market_type: str
    """Market type identifier, e.g. ``spot`` or ``futures``."""

    strategy: str
    """Registered strategy name."""

    symbol: str
    """Trading symbol."""

    timeframe: str
    """Candle timeframe."""

    quantity: float
    """Default order quantity."""

    live: bool
    """Whether the bot may submit live orders."""

    per_bot_cap: float
    """Per-bot notional cap passed to the risk guard."""

    global_cap: float
    """Global notional cap passed to the risk guard."""

    params: dict[str, Any]
    """Strategy-specific parameters."""

    creds: dict[str, object] = field(default_factory=dict)
    """Runtime venue credentials, loaded from the store when starting."""


@dataclass
class BotInstance:
    """Runtime state of a single supervised bot."""

    config: BotConfig
    """Static bot configuration."""

    status: str = "created"
    """Current lifecycle status, e.g. ``created``, ``running`` or ``stopped``."""

    runtime: StreamRuntime | None = None
    """Trading runtime once the bot is started."""

    task: asyncio.Task[None] | None = None
    """Background async task running the bot."""

    last_decision: str | None = None
    """Last decision text emitted by the strategy."""

    position: Position | None = None
    """Latest known position reported by the venue."""

    pnl: float = 0.0
    """Running profit-and-loss estimate."""

    venue: Any | None = None
    """Execution venue, retained after start for position polling."""

    hub: Any | None = None
    """Market-data hub, retained after start for mark-to-market pricing."""

    multiplier: float = 1.0
    """Contract multiplier used when marking PnL (1.0 for spot)."""

    contract: ContractSpec | None = None
    """Resolved contract metadata, set at start (#124).

    ``None`` until the bot starts. A running derivative always has one: a
    derivative whose metadata cannot be resolved is refused rather than
    started, so this being set is evidence the numbers came from the venue.
    """

    degraded: bool = False
    """Whether the bot is alive but starved of market data.

    Deliberately orthogonal to ``status``: a degraded bot is still ``running``
    and its lifecycle rules (idempotent start, the #109 patch refusal) are
    unchanged. Folding this into ``status`` would silently alter both.
    """

    degraded_reason: str | None = None
    """Why the bot is degraded, shown to the operator."""

    degraded_permanent: bool = False
    """Whether the degradation is a venue limitation a restart cannot fix."""

    state_seq: int = 0
    """Monotonic counter stamped on every published state event."""

    poll_task: asyncio.Task[None] | None = None
    """Background task refreshing position/PnL at a bounded cadence."""

    stream_listener: Any | None = None
    """Hub stream-exit listener registered while the bot runs."""

    lane: Any | None = None
    """Single-worker pool serializing this bot's blocking strategy/order work."""

    ledger: OrderLedger = field(default_factory=OrderLedger)
    """Projected order state, folded from this bot's persisted lifecycle log.

    In-memory view of durable history (#135). Rebuilt by replaying the log on
    restore, so it is a cache of the log rather than a second source of truth.
    """


class _HubFeed:
    """Adapter that exposes a market-data hub as a ``StreamingFeed``."""

    def __init__(
        self, hub: Any, cfg: BotConfig, warmup: list[Candle], lane: BlockingCalls
    ) -> None:
        """Initialize the feed adapter.

        Args:
            hub: Market-data hub providing candles and subscriptions.
            cfg: Bot configuration used for symbol/timeframe selection.
            warmup: Historical closed candles to replay on start.
            lane: This bot's single-worker pool. Bar processing runs there so
                the strategy and the synchronous order placement behind it stay
                off the event loop, while a single worker keeps bars in order.
        """
        self._hub = hub
        self._cfg = cfg
        self._warmup = list(warmup)
        self._handler: Callable[[Candle], None] | None = None
        self._stopped = asyncio.Event()
        self._subscribed = False
        self._lane = lane

    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        """Return the newest ``limit`` warm-up candles.

        Args:
            symbol: Trading symbol (ignored; uses configured symbol).
            timeframe: Candle timeframe (ignored; uses configured timeframe).
            limit: Maximum number of candles to return.

        Returns:
            Up to ``limit`` closed candles ordered oldest-first.
        """
        del symbol, timeframe
        return self._warmup[-limit:]

    def on_bar(self, handler: Callable[[Candle], None]) -> None:
        """Subscribe ``handler`` to new bars from the hub.

        Args:
            handler: Callback invoked for each new closed bar.
        """
        # The hub fans bars out from the stream task, on the event loop. Hand
        # each one straight to this bot's lane instead of processing inline:
        # the chain below runs strategy -> router -> venue.place_order, all
        # synchronous, and would otherwise block every other bot and the API.
        self._handler = lambda candle: self._lane.submit(handler, candle)
        self._hub.subscribe(self._cfg.symbol, self._cfg.timeframe, self._handler)
        self._subscribed = True

    async def run_async(self, *symbols: str) -> None:
        """Block until the feed is stopped.

        Args:
            symbols: Trading symbols to run (at least one is required).

        Raises:
            ValueError: If no symbols are provided.
        """
        if not symbols:
            raise ValueError("_HubFeed.run_async requires a symbol")
        await self._stopped.wait()

    def run(self, *symbols: str) -> None:
        """Synchronous entry point that runs ``run_async``.

        Args:
            symbols: Trading symbols to run.
        """
        asyncio.run(self.run_async(*symbols))

    def stop(self) -> None:
        """Unsubscribe from the hub and release the run loop."""
        if self._subscribed and self._handler is not None:
            self._hub.unsubscribe(self._cfg.symbol, self._cfg.timeframe, self._handler)
            self._subscribed = False
        self._stopped.set()


class BotSupervisor:
    """Owns bot instances, starts them and publishes their events."""

    def __init__(
        self,
        *,
        hub_factory: Callable[[BotConfig], Any],
        event_bus: EventBus,
        global_exposure: GlobalExposure,
        store: Any | None = None,
        state_poll_seconds: float = _STATE_POLL_SECONDS,
        workers: WorkerPools | None = None,
    ) -> None:
        """Initialize the supervisor.

        Args:
            hub_factory: Callable that creates a market-data hub for a config.
            event_bus: In-memory event bus used to broadcast bot events.
            global_exposure: Shared exposure tracker used by risk guards.
            store: Optional persistence layer; when set, order events are
                appended to its trade log in addition to being published.
            state_poll_seconds: How often a running bot re-marks its position
                and PnL. This bounds state traffic: the poll publishes only
                when the snapshot actually changed.
            workers: Per-venue thread pools used to keep synchronous exchange
                calls off the event loop (#111). Created if not supplied.

        Raises:
            ValueError: If ``state_poll_seconds`` is not positive.
        """
        if state_poll_seconds <= 0:
            raise ValueError("state_poll_seconds must be positive")
        self._hub_factory = hub_factory
        self._event_bus = event_bus
        self._global_exposure = global_exposure
        self._store = store
        self._state_poll_seconds = state_poll_seconds
        self._workers = workers if workers is not None else WorkerPools()
        self._bots: dict[str, BotInstance] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_snapshots: dict[str, tuple[Any, ...]] = {}

    @property
    def hub_factory(self) -> Callable[[BotConfig], Any]:
        """Return the market-data hub factory backing this supervisor."""
        return self._hub_factory

    @property
    def event_bus(self) -> EventBus:
        """Return the shared event bus."""
        return self._event_bus

    def create(self, cfg: BotConfig) -> BotInstance:
        """Create a new bot instance without starting it.

        Args:
            cfg: Bot configuration.

        Returns:
            The newly created bot instance.

        Raises:
            ValueError: If a bot with the same id already exists.
        """
        if cfg.id in self._bots:
            raise ValueError(f"bot {cfg.id!r} already exists")
        instance = BotInstance(config=cfg)
        self._bots[cfg.id] = instance
        self._publish_state(instance)
        return instance

    def restore(self) -> int:
        """Adopt every persisted bot config that is not already managed.

        Called during application startup so a restart does not lose the
        operator's bots. Restored bots are **not** started: they are given a
        non-running status and only begin trading when explicitly started, so a
        restart can never silently resume live orders.

        Returns:
            Number of bots adopted from the store.
        """
        if self._store is None:
            return 0
        try:
            configs = self._store.load_configs()
        except Exception:  # noqa: BLE001 - a bad store must not stop the service booting
            _log.exception("failed to load persisted bot configs")
            return 0
        restored = 0
        for cfg in configs:
            if cfg.id in self._bots:
                continue
            instance = BotInstance(config=cfg, status="stopped")
            self._rebuild_ledger(instance)
            self._bots[cfg.id] = instance
            self._publish_state(instance)
            restored += 1
        _log.info("restored %d persisted bot(s)", restored)
        return restored

    async def start(self, bot_id: str) -> None:
        """Build and start the bot identified by ``bot_id``.

        Args:
            bot_id: UUID of the bot to start.

        Raises:
            KeyError: If the bot does not exist.
            Exception: Any error from venue, strategy or runtime construction.
        """
        bot = self._require(bot_id)
        async with self._lock_for(bot_id):
            await self._start_locked(bot)

    async def _start_locked(self, bot: BotInstance) -> None:
        """Build and start ``bot``. The caller must hold its lifecycle lock.

        Args:
            bot: Bot to start.

        Raises:
            Exception: Any error from venue, strategy or runtime construction.
        """
        if bot.status == "running":
            return
        # Claim the bot before the first await, so a concurrent caller that
        # takes the lock afterwards sees a bot that is already under way.
        bot.status = "starting"
        # A fresh run starts from a clean data-health slate.
        bot.degraded = False
        bot.degraded_reason = None
        bot.degraded_permanent = False
        self._publish_state(bot)
        try:
            hub = self._hub_factory(bot.config)
            warmup = await hub.warmup(bot.config.symbol, bot.config.timeframe, _WARMUP_BARS)
            # One worker: bars for a bot must be processed in order, and a
            # bot stuck in a slow order must not delay any other bot.
            bot.lane = BlockingCalls(f"bot-{bot.config.id}", max_workers=1)
            feed = _HubFeed(hub, bot.config, warmup, bot.lane)
            venue = build_venue(
                bot.config.venue,
                bot.config.market_type,
                creds=bot.config.creds,
                live=bot.config.live,
            )
            spec = self._resolve_contract(bot, venue)
            multiplier = spec.contract_size
            bot.venue = venue
            bot.hub = hub
            bot.contract = spec
            bot.multiplier = multiplier
            router = SignalRouter.with_risk_guard(
                venue,
                per_bot_cap=bot.config.per_bot_cap,
                global_cap=bot.config.global_cap,
                global_state=self._global_exposure,
                price_source=lambda: hub.latest_price(bot.config.symbol, bot.config.timeframe),
                multiplier=multiplier,
            )
            strategy = build_strategy(
                bot.config.strategy,
                StrategyContext(
                    symbol=bot.config.symbol,
                    timeframe=bot.config.timeframe,
                    quantity=bot.config.quantity,
                    data_feed=hub,
                    params=bot.config.params,
                ),
            )
            bot.runtime = StreamRuntime(
                feed=feed,
                strategy=strategy,
                router=router,
                symbol=bot.config.symbol,
                timeframe=bot.config.timeframe,
                warmup_bars=len(warmup),
                on_event=lambda text: self._handle_event(bot, text),
                run_blocking=bot.lane.run,
            )
            bot.status = "running"
            bot.task = asyncio.create_task(bot.runtime.start_async(install_signals=False))
            bot.task.add_done_callback(lambda task: self._task_done(bot, task))
            self._attach_stream_listener(bot, hub)
            bot.poll_task = asyncio.create_task(self._poll_state(bot))
            self._publish_state(bot)
        except Exception:
            self._release_partial(bot)
            bot.status = "failed"
            self._publish_state(bot)
            raise

    async def stop(self, bot_id: str) -> None:
        """Stop the bot identified by ``bot_id``.

        Idempotent: stopping an already-stopped or never-started bot is a
        no-op, and concurrent stops clean up exactly once because they
        serialize on the bot's lifecycle lock.

        Args:
            bot_id: UUID of the bot to stop.

        Raises:
            KeyError: If the bot does not exist.
        """
        bot = self._require(bot_id)
        async with self._lock_for(bot_id):
            if bot.status in ("created", "stopped", "failed"):
                # Idempotent, but clearing a created/failed bot is a real
                # transition the operator should see.
                if bot.status != "stopped":
                    bot.status = "stopped"
                    self._publish_state(bot)
                return
            bot.status = "stopping"
            self._publish_state(bot)
            self._detach_stream_listener(bot)
            await self._cancel_poll(bot)
            if bot.runtime is not None:
                bot.runtime.stop()
            task = bot.task
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            bot.task = None
            bot.runtime = None
            self._release_lane(bot)
            bot.status = "stopped"
            self._publish_state(bot)

    def _lock_for(self, bot_id: str) -> asyncio.Lock:
        """Return the lifecycle lock for ``bot_id``, creating it on first use.

        Every start/stop for one bot serializes on this lock, so concurrent
        requests cannot build two runtimes or cancel a half-built one.

        Args:
            bot_id: UUID of the bot.

        Returns:
            The bot's lifecycle lock.
        """
        lock = self._locks.get(bot_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[bot_id] = lock
        return lock

    def _release_partial(self, bot: BotInstance) -> None:
        """Drop everything a failed start had attached to ``bot``.

        Without this a failed start leaves a venue and hub bound to the
        instance, so the next retry reuses half-built state.

        Args:
            bot: Bot whose partially built runtime should be released.
        """
        self._detach_stream_listener(bot)
        poll_task = bot.poll_task
        if poll_task is not None and not poll_task.done():
            poll_task.cancel()
        bot.poll_task = None
        runtime = bot.runtime
        if runtime is not None:
            try:
                runtime.stop()
            except Exception:  # noqa: BLE001 - cleanup must not mask the original error
                _log.exception("failed to stop partially built runtime for bot %s", bot.config.id)
        bot.runtime = None
        bot.task = None
        bot.venue = None
        bot.hub = None
        bot.multiplier = 1.0
        self._release_lane(bot)

    def _release_lane(self, bot: BotInstance) -> None:
        """Shut down ``bot``'s worker lane, if it has one.

        Not waited on: a lane may be parked in a hung exchange call, and a
        stop must not inherit that wait.

        Args:
            bot: Bot whose lane should be released.
        """
        lane = bot.lane
        bot.lane = None
        if lane is not None:
            lane.shutdown(wait=False)

    def shutdown_workers(self) -> None:
        """Release every venue worker pool.

        Called during application shutdown. Does not wait: a pool may be parked
        in a hung exchange call, and shutdown must not inherit that wait.
        """
        self._workers.shutdown(wait=False)

    def _venue_workers(self, bot: BotInstance):
        """Return the thread pool dedicated to ``bot``'s venue.

        Keyed per venue and market type so one stuck exchange exhausts only
        its own workers, never another venue's or the event loop.

        Args:
            bot: Bot whose venue pool is wanted.

        Returns:
            The venue's :class:`BlockingCalls` pool.
        """
        return self._workers.for_name(f"{bot.config.venue}:{bot.config.market_type}")

    async def _cancel_poll(self, bot: BotInstance) -> None:
        """Cancel and await ``bot``'s state-poll task, if one is running.

        Args:
            bot: Bot whose poll task should be torn down.
        """
        task = bot.poll_task
        bot.poll_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _poll_state(self, bot: BotInstance) -> None:
        """Re-mark ``bot``'s position and PnL until the bot is stopped.

        PnL moves with the price, not only with fills, so without this the
        operator sees a stale number between orders. Publishing only on change
        keeps the fan-out proportional to real movement rather than to the
        clock.

        Args:
            bot: Running bot to poll.
        """
        while True:
            await asyncio.sleep(self._state_poll_seconds)
            try:
                # Chase outstanding orders before reading the position, so a
                # fill discovered here is reflected in the same pass rather
                # than a cadence later (#135).
                await self.reconcile_open_orders(bot)
                # get_position() is a blocking exchange call; run it on the
                # venue's pool so a slow venue cannot stall the loop.
                await self._venue_workers(bot).run(self._refresh_position, bot)
                self._refresh_pnl(bot)
                self._publish_state_if_changed(bot)
            except BlockingCallTimeout:
                _log.warning("position refresh timed out for bot %s", bot.config.id)
            except Exception:  # noqa: BLE001 - a bad poll must not stop the bot
                _log.exception("state poll failed for bot %s", bot.config.id)

    def _attach_stream_listener(self, bot: BotInstance, hub: Any) -> None:
        """Subscribe ``bot`` to unexpected stream exits on its hub.

        Hubs are shared between bots, so the listener filters on the bot's own
        symbol and timeframe. Feeds without the signal (older hubs, test
        doubles) simply never degrade.

        Args:
            bot: Bot to mark degraded when its stream dies.
            hub: Market-data hub backing the bot.
        """
        register = getattr(hub, "add_stream_listener", None)
        if not callable(register):
            return

        def _on_stream_exit(
            symbol: str, timeframe: str, reason: str, permanent: bool = False
        ) -> None:
            if symbol != bot.config.symbol or timeframe != bot.config.timeframe:
                return
            if bot.status not in ("running", "starting"):
                return
            bot.degraded = True
            bot.degraded_reason = reason
            bot.degraded_permanent = permanent
            self._publish_state(bot)

        bot.stream_listener = _on_stream_exit
        register(_on_stream_exit)

    def _detach_stream_listener(self, bot: BotInstance) -> None:
        """Remove ``bot``'s stream listener from its hub, if registered.

        The hub outlives the bot, so leaving the listener attached would
        degrade a bot that is no longer running.

        Args:
            bot: Bot whose listener should be removed.
        """
        listener = bot.stream_listener
        bot.stream_listener = None
        if listener is None or bot.hub is None:
            return
        remove = getattr(bot.hub, "remove_stream_listener", None)
        if callable(remove):
            remove(listener)

    def _snapshot(self, bot: BotInstance) -> tuple[Any, ...]:
        """Return the comparable state tuple used to suppress no-op events.

        Args:
            bot: Bot to snapshot.

        Returns:
            Tuple of every field carried by a ``BotStateEvent``.
        """
        return (
            bot.status,
            None if bot.position is None else bot.position.model_dump_json(),
            bot.pnl,
            bot.last_decision,
            bot.degraded,
            bot.degraded_reason,
            bot.degraded_permanent,
        )

    def _publish_state(self, bot: BotInstance) -> None:
        """Broadcast ``bot``'s current state, stamping the next sequence number.

        Args:
            bot: Bot whose state to broadcast.
        """
        bot.state_seq += 1
        self._last_snapshots[bot.config.id] = self._snapshot(bot)
        self._event_bus.publish(
            BotStateEvent(
                bot_id=bot.config.id,
                seq=bot.state_seq,
                status=bot.status,
                position=None if bot.position is None else bot.position.model_dump(),
                pnl=bot.pnl,
                last_decision=bot.last_decision,
                degraded=bot.degraded,
                degraded_reason=bot.degraded_reason,
                degraded_permanent=bot.degraded_permanent,
            )
        )

    def _publish_state_if_changed(self, bot: BotInstance) -> None:
        """Broadcast ``bot``'s state only when it differs from the last one sent.

        Args:
            bot: Bot whose state to broadcast.
        """
        if self._last_snapshots.get(bot.config.id) == self._snapshot(bot):
            return
        self._publish_state(bot)

    async def remove(self, bot_id: str) -> BotInstance:
        """Drop a bot from the supervisor.

        Serialized on the bot's lifecycle lock so a delete cannot interleave
        with a start (#126): without it a delete could land between a start's
        checks and its task creation, leaving a running runtime with no bot to
        own it.

        Args:
            bot_id: UUID of the bot to remove.

        Returns:
            The removed bot instance.

        Raises:
            KeyError: If the bot does not exist.
            ValueError: If the bot is running or mid-transition. It may hold an
                open position, and deleting it would strand that position with
                nothing left to manage it.
        """
        bot = self._require(bot_id)
        async with self._lock_for(bot_id):
            if bot.status not in ("created", "stopped", "failed"):
                raise ValueError(
                    f"bot {bot_id} is {bot.status}; stop it before deleting"
                )
            self._release_partial(bot)
            self._bots.pop(bot_id, None)
            self._last_snapshots.pop(bot_id, None)
        self._locks.pop(bot_id, None)
        return bot

    def list(self) -> list[BotInstance]:
        """Return all managed bot instances."""
        return list(self._bots.values())

    def get(self, bot_id: str) -> BotInstance | None:
        """Return the bot identified by ``bot_id`` if it exists.

        Args:
            bot_id: UUID of the bot.

        Returns:
            The bot instance, or ``None`` when not found.
        """
        return self._bots.get(bot_id)

    def _require(self, bot_id: str) -> BotInstance:
        """Return ``bot_id`` or raise ``KeyError``.

        Args:
            bot_id: UUID of the bot.

        Returns:
            The bot instance.

        Raises:
            KeyError: If the bot does not exist.
        """
        bot = self.get(bot_id)
        if bot is None:
            raise KeyError(f"unknown bot {bot_id!r}")
        return bot

    def _task_done(self, bot: BotInstance, task: asyncio.Task[None]) -> None:
        """Update bot status when its runtime task finishes.

        Args:
            bot: Bot whose task completed.
            task: Completed async task.
        """
        # A deliberate stop already owns the outcome; don't overwrite it.
        if bot.task is not task or bot.status in ("stopped", "stopping"):
            return
        if task.cancelled():
            bot.status = "stopped"
        elif task.exception() is not None:
            bot.status = "failed"
        else:
            bot.status = "stopped"
        # Without this the UI keeps showing a bot that died as running.
        self._publish_state(bot)

    def _persist_trade(self, bot: BotInstance, payload: dict[str, Any], event: OrderEvent) -> None:
        """Append this order's lifecycle events to the store's log.

        Writes ledger events -- ``submitted``, ``order_status``, ``dry_run``,
        ``rejected``, ``canceled`` -- rather than the single flat summary row
        this used to write. That row named every outcome a "trade", including
        dry runs and orders the venue had merely acknowledged, which is the
        misrepresentation #135 exists to remove.

        The events share the existing trade log rather than opening a second
        one, so segment rotation, #122's paging and #163's archive-on-delete
        all keep working unchanged. Records are told apart by ``kind``; rows
        written before this change have none and are read as legacy history.

        A payload carrying no order -- a close, or a legacy event -- yields no
        ledger events. The flat summary row is still written for those so the
        operator's history keeps a trace of what happened.

        Persistence failures are logged and swallowed so a disk error never
        crashes a running bot.

        Args:
            bot: Bot that produced the order.
            payload: Raw runtime order payload (carries the order and result).
            event: Structured order event published to the bus.
        """
        if self._store is None:
            return

        records = events_from_payload(payload, bot_id=bot.config.id)
        if not records:
            records = [{
                "bot_id": bot.config.id,
                "action": event.action,
                "status": event.status,
                "ok": event.ok,
                "order_id": event.order_id,
                "symbol": str(payload.get("symbol", bot.config.symbol)),
                "ts": int(payload.get("ts", 0)),
            }]

        self._write_events(bot, records)

    def _write_events(self, bot: BotInstance, records: list[dict[str, Any]]) -> None:
        """Persist lifecycle events, then fold them into the bot's ledger.

        For events that exist only in the moment they happen -- a submission,
        the venue's answer to it -- the log is the source of truth and the
        ledger is a cache of it, so the write happens first. An event that
        failed to persist must not be visible in the projection, or a restart
        would silently lose it.

        Args:
            bot: Bot the events belong to.
            records: Lifecycle events to write, oldest first.
        """
        if self._store is None:
            return
        for record in records:
            try:
                self._store.append_trade(bot.config.id, record)
            except Exception:  # pragma: no cover - defensive; disk errors shouldn't kill the bot
                _log.exception("failed to persist trade for bot %s", bot.config.id)
                continue
            bot.ledger.apply(record)

    def _write_polled_events(self, bot: BotInstance, records: list[dict[str, Any]]) -> None:
        """Fold polled events into the ledger, persisting only what changed.

        The ordering here is deliberately the opposite of ``_write_events``,
        for a reason specific to polling: a poll re-reads state the venue still
        holds, so anything lost to a disk error is re-derived by the next poll.
        That makes it safe to let the projection decide what is worth writing.

        And it has to decide, because polls repeat. An order that sits open
        answers every poll with the same cumulative snapshot; persisting each
        one would append a redundant record every few seconds for as long as
        the order lives, for no information at all. ``apply`` already reports
        whether an event moved the projection, so that is the filter.

        Args:
            bot: Bot the events belong to.
            records: Lifecycle events derived from a status poll, oldest first.
        """
        if self._store is None:
            return
        for record in records:
            if not bot.ledger.apply(record):
                continue
            try:
                self._store.append_trade(bot.config.id, record)
            except Exception:  # pragma: no cover - defensive; the next poll re-derives it
                _log.exception("failed to persist polled event for bot %s", bot.config.id)

    def _resolve_contract(self, bot: BotInstance, venue: Any) -> ContractSpec:
        """Resolve the bot's contract metadata, failing closed for derivatives.

        Exposure and PnL are ``quantity x price x contract_size``, so a bot
        whose contract size is unknown cannot be risk-managed at all. Before
        #124 that case silently used ``1.0``, which is wrong by the contract
        size -- 5x for a CME Bitcoin future, 0.1x for a micro -- and produces
        numbers too ordinary-looking to catch by eye.

        Spot needs no lookup: one unit is one unit of the base asset, the one
        case where ``1.0`` is a fact. A venue that cannot describe its own
        derivative stops the bot starting.

        Args:
            bot: Bot being started.
            venue: Its freshly built execution venue.

        Returns:
            The validated contract spec.

        Raises:
            ContractMetadataError: If this is a derivative and its metadata
                cannot be resolved.
        """
        symbol = bot.config.symbol
        resolve = getattr(venue, "contract_spec", None)
        if callable(resolve):
            return cast(ContractSpec, resolve(symbol))

        if _is_derivative_market(bot.config.market_type):
            raise ContractMetadataError(
                f"{symbol}: venue {bot.config.venue!r} cannot report contract "
                f"metadata for {bot.config.market_type!r}, so exposure cannot "
                "be computed safely"
            )
        return spot_spec(symbol, quote_currency=_quote_currency(symbol))

    def _rebuild_ledger(self, bot: BotInstance) -> None:
        """Rebuild ``bot``'s order ledger by replaying its persisted log.

        The log is the source of truth and the ledger is a projection of it, so
        recovery after a restart is simply replay -- which is the property
        event sourcing was chosen for. Orders that were still live when the
        process died come back non-terminal, which puts them straight back on
        the reconciliation worklist.

        Nothing is written here. Folding a log is a read, and appending to it
        during replay would grow the history on every boot.

        A replay that fails, or a record that will not fold, is logged and
        skipped rather than raised: a corrupt row must cost that row, not the
        bot and not the service's ability to boot (#108's isolation applied to
        the trade log).

        Args:
            bot: Bot whose ledger to rebuild.
        """
        replay = getattr(self._store, "replay_trades", None)
        if not callable(replay):
            return
        try:
            records = list(cast(Iterable[dict[str, Any]], replay(bot.config.id)))
        except Exception:  # noqa: BLE001 - a bad log must not stop the service booting
            _log.exception("failed to replay order log for bot %s", bot.config.id)
            return

        ledger = OrderLedger()
        for record in records:
            try:
                ledger.apply(record)
            except Exception:  # noqa: BLE001 - one bad row must not cost the history
                _log.exception("skipping malformed order record for bot %s", bot.config.id)
        bot.ledger = ledger

    async def reconcile_open_orders(self, bot: BotInstance) -> None:
        """Ask the venue about every order that is not yet terminal.

        Venues acknowledge before they fill -- Tradovate always, ccxt often --
        so an order left alone stays ``submitted`` forever and the position it
        will create stays invisible. This closes that gap by going back and
        asking.

        Only non-terminal orders are polled, so the cost is proportional to
        what is actually outstanding rather than to history. Orders with no
        venue id never reached the venue and cannot be asked about.

        A venue that raises, or cannot answer at all, leaves its orders open.
        That is deliberate: not knowing an order's state is different from
        knowing it failed, and treating the two alike would mark a live order
        dead in our books while it keeps working at the venue.

        Args:
            bot: Bot whose outstanding orders to reconcile.
        """
        venue = bot.venue
        fetch = getattr(venue, "fetch_order", None)
        if venue is None or not callable(fetch):
            return

        for order in bot.ledger.open_orders(bot_id=bot.config.id):
            if not order.venue_order_id:
                continue
            try:
                result = await self._venue_workers(bot).run(
                    fetch, order.venue_order_id, order.symbol
                )
            except BlockingCallTimeout:
                _log.warning(
                    "order status poll timed out for bot %s order %s",
                    bot.config.id, order.client_order_id,
                )
                continue
            except Exception:  # noqa: BLE001 - an unreachable venue is not evidence
                _log.exception(
                    "order status poll failed for bot %s order %s",
                    bot.config.id, order.client_order_id,
                )
                continue

            self._write_polled_events(
                bot,
                events_from_status(
                    order.client_order_id,
                    # The worker pool is untyped, so the venue's OrderResult
                    # comes back as `object`.
                    cast(OrderResult, result),
                    ts=order.updated_ts or order.created_ts,
                ),
            )

    def _handle_event(self, bot: BotInstance, raw: str) -> None:
        """Parse a runtime event and publish it to the event bus.

        Args:
            bot: Bot that emitted the event.
            raw: Raw JSON or text payload from the runtime.
        """
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            bot.last_decision = raw
            return
        if payload.get("type") == "order":
            order_event = OrderEvent(
                bot_id=bot.config.id,
                action=str(payload.get("action", "")),
                status=str(payload.get("status", "")),
                ok=bool(payload.get("ok", False)),
                order_id=payload.get("order_id"),
            )
            self._event_bus.publish(order_event)
            self._persist_trade(bot, payload, order_event)
            # A fill may have changed the position; refresh it, then mark PnL.
            self._refresh_position(bot)
            self._refresh_pnl(bot)
            self._publish_state_if_changed(bot)
            return
        text = str(payload.get("text", raw))
        bot.last_decision = text
        # Mark PnL to the latest price on every decision (price moves between fills).
        self._refresh_pnl(bot)
        self._event_bus.publish(DecisionEvent(
            bot_id=bot.config.id,
            symbol=bot.config.symbol,
            ts=int(payload.get("ts", 0)),
            text=text,
        ))
        self._publish_state_if_changed(bot)

    def _refresh_position(self, bot: BotInstance) -> None:
        """Update ``bot.position`` from the venue, tolerating venue errors.

        Args:
            bot: Bot whose position to refresh.
        """
        if bot.venue is None:
            return
        try:
            bot.position = bot.venue.get_position(bot.config.symbol)
        except Exception:  # pragma: no cover - defensive; venue errors shouldn't kill the bot
            _log.exception("failed to read position for bot %s", bot.config.id)

    def _refresh_pnl(self, bot: BotInstance) -> None:
        """Mark ``bot.pnl`` to market from the hub's latest price.

        Unrealized PnL is ``sign * (price - entry) * size * multiplier`` where
        ``sign`` is +1 long / -1 short. A flat position resets PnL to zero; a
        missing price leaves the last value untouched.

        Args:
            bot: Bot whose PnL to recompute.
        """
        pos = bot.position
        if pos is None or pos.side is PositionSide.flat:
            bot.pnl = 0.0
            return
        if bot.hub is None:
            return
        price = bot.hub.latest_price(bot.config.symbol, bot.config.timeframe)
        if price is None:
            return
        sign = 1.0 if pos.side is PositionSide.long else -1.0
        bot.pnl = sign * (float(price) - pos.entry_price) * pos.size * bot.multiplier
