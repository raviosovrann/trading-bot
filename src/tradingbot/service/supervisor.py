"""Bot supervisor that manages bot lifecycle, wiring and event publishing."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, cast

from ..models import Candle, Position, PositionSide
from ..router import SignalRouter
from ..runtime import StreamRuntime
from ..strategies import StrategyContext
from ..stream import StreamingFeed
from .events import DecisionEvent, EventBus, OrderEvent
from .registry import build_strategy, build_venue
from .risk import GlobalExposure

_log = logging.getLogger(__name__)

_WARMUP_BARS = 220
"""Default number of warm-up bars requested when starting a bot."""


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


class _HubFeed:
    """Adapter that exposes a market-data hub as a ``StreamingFeed``."""

    def __init__(self, hub: Any, cfg: BotConfig, warmup: list[Candle]) -> None:
        """Initialize the feed adapter.

        Args:
            hub: Market-data hub providing candles and subscriptions.
            cfg: Bot configuration used for symbol/timeframe selection.
            warmup: Historical closed candles to replay on start.
        """
        self._hub = hub
        self._cfg = cfg
        self._warmup = list(warmup)
        self._handler: Callable[[Candle], None] | None = None
        self._stopped = asyncio.Event()
        self._subscribed = False

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
        self._handler = handler
        self._hub.subscribe(self._cfg.symbol, self._cfg.timeframe, handler)
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
    ) -> None:
        """Initialize the supervisor.

        Args:
            hub_factory: Callable that creates a market-data hub for a config.
            event_bus: In-memory event bus used to broadcast bot events.
            global_exposure: Shared exposure tracker used by risk guards.
            store: Optional persistence layer; when set, order events are
                appended to its trade log in addition to being published.
        """
        self._hub_factory = hub_factory
        self._event_bus = event_bus
        self._global_exposure = global_exposure
        self._store = store
        self._bots: dict[str, BotInstance] = {}

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
        return instance

    async def start(self, bot_id: str) -> None:
        """Build and start the bot identified by ``bot_id``.

        Args:
            bot_id: UUID of the bot to start.

        Raises:
            KeyError: If the bot does not exist.
            Exception: Any error from venue, strategy or runtime construction.
        """
        bot = self._require(bot_id)
        if bot.status == "running":
            return
        try:
            hub = self._hub_factory(bot.config)
            warmup = await hub.warmup(bot.config.symbol, bot.config.timeframe, _WARMUP_BARS)
            feed = _HubFeed(hub, bot.config, warmup)
            venue = build_venue(
                bot.config.venue,
                bot.config.market_type,
                creds=bot.config.creds,
                live=bot.config.live,
            )
            multiplier_factory = getattr(venue, "contract_multiplier", None)
            multiplier = (
                float(cast(Any, multiplier_factory(bot.config.symbol)))
                if callable(multiplier_factory)
                else 1.0
            )
            bot.venue = venue
            bot.hub = hub
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
            )
            bot.status = "running"
            bot.task = asyncio.create_task(bot.runtime.start_async(install_signals=False))
            bot.task.add_done_callback(lambda task: self._task_done(bot, task))
        except Exception:
            bot.status = "failed"
            raise

    async def stop(self, bot_id: str) -> None:
        """Stop the bot identified by ``bot_id``.

        Args:
            bot_id: UUID of the bot to stop.

        Raises:
            KeyError: If the bot does not exist.
        """
        bot = self._require(bot_id)
        if bot.runtime is not None:
            bot.runtime.stop()
        if bot.task is not None and not bot.task.done():
            bot.task.cancel()
            try:
                await bot.task
            except asyncio.CancelledError:
                pass
        bot.status = "stopped"

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
        if bot.task is not task or bot.status == "stopped":
            return
        if task.cancelled():
            bot.status = "stopped"
            return
        if task.exception() is not None:
            bot.status = "failed"
        else:
            bot.status = "stopped"

    def _persist_trade(self, bot: BotInstance, payload: dict[str, Any], event: OrderEvent) -> None:
        """Append an order event to the store's trade log, if a store is set.

        Persistence failures are logged and swallowed so a disk error never
        crashes a running bot.

        Args:
            bot: Bot that produced the order.
            payload: Raw runtime order payload (carries symbol/ts).
            event: Structured order event published to the bus.
        """
        if self._store is None:
            return
        record = {
            "bot_id": bot.config.id,
            "action": event.action,
            "status": event.status,
            "ok": event.ok,
            "order_id": event.order_id,
            "symbol": str(payload.get("symbol", bot.config.symbol)),
            "ts": int(payload.get("ts", 0)),
        }
        try:
            self._store.append_trade(bot.config.id, record)
        except Exception:  # pragma: no cover - defensive; disk errors shouldn't kill the bot
            _log.exception("failed to persist trade for bot %s", bot.config.id)

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
