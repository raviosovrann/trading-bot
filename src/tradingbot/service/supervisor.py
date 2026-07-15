from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Callable, cast

from ..models import Candle, Position
from ..router import SignalRouter
from ..runtime import StreamRuntime
from ..strategies import StrategyContext
from ..stream import StreamingFeed
from .events import DecisionEvent, EventBus, OrderEvent
from .registry import build_strategy, build_venue
from .risk import GlobalExposure

_WARMUP_BARS = 220


@dataclass
class BotConfig:
    id: str
    venue: str
    market_type: str
    strategy: str
    symbol: str
    timeframe: str
    quantity: float
    live: bool
    per_bot_cap: float
    global_cap: float
    params: dict[str, Any]
    creds: dict[str, object] = field(default_factory=dict)


@dataclass
class BotInstance:
    config: BotConfig
    status: str = "created"
    runtime: StreamRuntime | None = None
    task: asyncio.Task[None] | None = None
    last_decision: str | None = None
    position: Position | None = None
    pnl: float = 0.0


class _HubFeed:
    def __init__(self, hub: Any, cfg: BotConfig, warmup: list[Candle]) -> None:
        self._hub = hub
        self._cfg = cfg
        self._warmup = list(warmup)
        self._handler: Callable[[Candle], None] | None = None
        self._stopped = asyncio.Event()
        self._subscribed = False

    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        del symbol, timeframe
        return self._warmup[-limit:]

    def on_bar(self, handler: Callable[[Candle], None]) -> None:
        self._handler = handler
        self._hub.subscribe(self._cfg.symbol, self._cfg.timeframe, handler)
        self._subscribed = True

    async def run_async(self, *symbols: str) -> None:
        if not symbols:
            raise ValueError("_HubFeed.run_async requires a symbol")
        await self._stopped.wait()

    def run(self, *symbols: str) -> None:
        asyncio.run(self.run_async(*symbols))

    def stop(self) -> None:
        if self._subscribed and self._handler is not None:
            self._hub.unsubscribe(self._cfg.symbol, self._cfg.timeframe, self._handler)
            self._subscribed = False
        self._stopped.set()


class BotSupervisor:
    def __init__(
        self,
        *,
        hub_factory: Callable[[BotConfig], Any],
        event_bus: EventBus,
        global_exposure: GlobalExposure,
    ) -> None:
        self._hub_factory = hub_factory
        self._event_bus = event_bus
        self._global_exposure = global_exposure
        self._bots: dict[str, BotInstance] = {}

    @property
    def event_bus(self) -> EventBus:
        return self._event_bus

    def create(self, cfg: BotConfig) -> BotInstance:
        if cfg.id in self._bots:
            raise ValueError(f"bot {cfg.id!r} already exists")
        instance = BotInstance(config=cfg)
        self._bots[cfg.id] = instance
        return instance

    async def start(self, bot_id: str) -> None:
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
        return list(self._bots.values())

    def get(self, bot_id: str) -> BotInstance | None:
        return self._bots.get(bot_id)

    def _require(self, bot_id: str) -> BotInstance:
        bot = self.get(bot_id)
        if bot is None:
            raise KeyError(f"unknown bot {bot_id!r}")
        return bot

    def _task_done(self, bot: BotInstance, task: asyncio.Task[None]) -> None:
        if bot.task is not task or bot.status == "stopped":
            return
        if task.cancelled():
            bot.status = "stopped"
            return
        if task.exception() is not None:
            bot.status = "failed"
        else:
            bot.status = "stopped"

    def _handle_event(self, bot: BotInstance, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            bot.last_decision = raw
            return
        if payload.get("type") == "order":
            self._event_bus.publish(OrderEvent(
                bot_id=bot.config.id,
                action=str(payload.get("action", "")),
                status=str(payload.get("status", "")),
                ok=bool(payload.get("ok", False)),
                order_id=payload.get("order_id"),
            ))
            return
        text = str(payload.get("text", raw))
        bot.last_decision = text
        self._event_bus.publish(DecisionEvent(
            bot_id=bot.config.id,
            symbol=bot.config.symbol,
            ts=int(payload.get("ts", 0)),
            text=text,
        ))
