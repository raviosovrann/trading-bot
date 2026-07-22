"""A slow exchange must not freeze the API, sockets or other bots (#111).

Every test here drives the real ASGI app against a venue/feed that blocks the
calling thread, the way ccxt does on a slow exchange.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from tradingbot.models import Action, Candle, Order, OrderResult, OrderType, Position, PositionSide, Signal
from tradingbot.service.api import create_app
from tradingbot.service.auth import hash_password
from tradingbot.service.datahub import MarketDataHub
from tradingbot.service.events import EventBus
from tradingbot.service.ratelimit import RateLimiter
from tradingbot.service.exposure import ExposureTracker
from tradingbot.service.store import BotStore
from tradingbot.service.supervisor import BotSupervisor

_TOKEN = "test-token"
_TOKEN_HASH = hashlib.sha256(_TOKEN.encode()).hexdigest()

_BOT_PAYLOAD = {
    "venue": "coinbase",
    "market_type": "spot",
    "strategy": "example",
    "symbol": "BTC/USD",
    "timeframe": "1m",
    "quantity": 0.1,
    "live": False,
    "per_bot_cap": 1000.0,
    "global_cap": 10000.0,
    "params": {},
}

# How long the fake exchange blocks. The assertions are causal — did the API
# answer *while* the call was in flight — not a latency budget, so this only
# needs to be long enough to observe, not long enough to time.
# 0.5s leaves ~50x margin over the observed in-process request latency (~10ms)
# while cutting the old 1.5s. Note the failure direction is safe: too short
# makes these fail, never pass vacuously, because the assertion requires the
# response to land before the block returns.
BLOCK_SECONDS = 0.5


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


def _candle(ts: int = 1, close: float = 100.0) -> Candle:
    return Candle(timestamp=ts, open=close, high=close, low=close, close=close, volume=1.0)


class _BlockingCandleFeed:
    """Candle feed whose REST warmup blocks the calling thread, like ccxt.

    Records when the blocking call is entered and left, so a test can assert
    the API answered *while the call was still in flight* rather than relying
    on a wall-clock budget, which would be racy.
    """

    def __init__(self, seconds: float = BLOCK_SECONDS) -> None:
        self.seconds = seconds
        self.warmups = 0
        self.entered_at: float | None = None
        self.exited_at: float | None = None

    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        del symbol, timeframe, limit
        self.warmups += 1
        self.entered_at = time.monotonic()
        time.sleep(self.seconds)
        self.exited_at = time.monotonic()
        return [_candle()]

    def latest_closed_candle(self, symbol: str, timeframe: str) -> Candle:
        del symbol, timeframe
        return _candle()


class _IdleStream:
    """Stream feed that never delivers a bar, so tests stay deterministic."""

    def __init__(self) -> None:
        self._stopped = asyncio.Event()

    def on_bar(self, handler) -> None:
        pass

    def on_bar_for(self, symbol: str, handler) -> None:
        pass

    async def run_async(self, *symbols: str) -> None:
        await self._stopped.wait()

    def stop(self) -> None:
        self._stopped.set()

    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        del symbol, timeframe, limit
        return []


def _hub_with(feed: _BlockingCandleFeed) -> MarketDataHub:
    """Build a real hub over a blocking candle feed."""
    return MarketDataHub(
        stream_feed=cast(Any, _IdleStream()),
        candle_feed=cast(Any, feed),
        limiter=RateLimiter(1000, 1000),
    )


class _BlockingVenue:
    """Venue whose position/order calls block, like a slow exchange."""

    def __init__(self, seconds: float = BLOCK_SECONDS) -> None:
        self.seconds = seconds
        self.entered_at: float | None = None
        self.exited_at: float | None = None

    def place_order(self, order: Order) -> OrderResult:
        self.entered_at = time.monotonic()
        time.sleep(self.seconds)
        self.exited_at = time.monotonic()
        return OrderResult(ok=True, order_id="o1", status="filled", filled_qty=order.qty, raw={})

    def close_position(self, symbol: str) -> OrderResult:
        del symbol
        time.sleep(self.seconds)
        return OrderResult(ok=True, order_id=None, status="no position", filled_qty=0.0, raw={})

    def get_position(self, symbol: str) -> Position | None:
        del symbol
        self.entered_at = time.monotonic()
        time.sleep(self.seconds)
        self.exited_at = time.monotonic()
        return None

    def health_check(self) -> bool:
        time.sleep(self.seconds)
        return True


class _IdleStrategy:
    def on_bar(self, candles) -> Signal | None:
        del candles
        return None


class _SignalStrategy:
    def on_bar(self, candles) -> Signal | None:
        del candles
        return Signal(
            strategy="test",
            action=Action.buy,
            symbol="BTC/USD",
            order_type=OrderType.market,
            quantity=0.1,
            position_side=PositionSide.long,
        )


def _store(tmp_path: Path) -> BotStore:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "users.json").write_text(
        json.dumps({
            "users": [{
                "username": "operator",
                "token_hash": _TOKEN_HASH,
                "password_hash": hash_password("pw"),
            }]
        })
    )
    (data_dir / "trades").mkdir()
    store = BotStore(data_dir)
    store.save_secrets("coinbase", "spot", {"api_key": "k", "api_secret": "s"})
    return store


def _build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, strategy=None, feed=None, venue=None):
    """Build an app over a real hub whose candle feed and venue block."""
    feed = feed if feed is not None else _BlockingCandleFeed()
    hub = _hub_with(feed)
    venue = venue or _BlockingVenue()
    monkeypatch.setattr("tradingbot.service.supervisor.build_venue", lambda *a, **k: venue)
    monkeypatch.setattr(
        "tradingbot.service.supervisor.build_strategy",
        lambda *a, **k: strategy if strategy is not None else _IdleStrategy(),
    )
    supervisor = BotSupervisor(
        hub_factory=lambda cfg: hub,
        event_bus=EventBus(),
        exposure=ExposureTracker(),
        state_poll_seconds=0.05,
    )
    store = _store(tmp_path)
    return create_app(store=store, supervisor=supervisor), supervisor, feed


async def _await_entry(subject, timeout: float = 3.0) -> None:
    """Wait until ``subject`` has entered its blocking call.

    If the call is running on the event loop this poll cannot run at all until
    it finishes — which is exactly the failure the tests then catch, because
    ``exited_at`` will already be set.
    """
    deadline = time.monotonic() + timeout
    while subject.entered_at is None and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert subject.entered_at is not None, "the blocking call never started"


async def _get_while_blocked(ac: httpx.AsyncClient, url: str, subject) -> int:
    """GET ``url`` and assert it completed before the blocking call returned.

    This is deterministic where a wall-clock budget is not: either the response
    landed while the exchange call was still in flight (loop free) or it did
    not (loop blocked).
    """
    response = await ac.get(url, headers=_auth())
    done_at = time.monotonic()
    exited_at = subject.exited_at
    assert exited_at is None or done_at < exited_at, (
        f"{url} only answered after the blocking exchange call returned — "
        "the call is still on the event loop"
    )
    return response.status_code


@pytest.mark.asyncio
async def test_api_stays_responsive_while_a_warmup_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a bot starting against a frozen exchange does not stall the API."""
    app, _supervisor, feed = _build(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        bot_id = (await ac.post("/api/bots", json=_BOT_PAYLOAD, headers=_auth())).json()["id"]

        start = asyncio.create_task(ac.post(f"/api/bots/{bot_id}/start", headers=_auth()))
        await _await_entry(feed)

        assert await _get_while_blocked(ac, "/api/bots", feed) == 200

        await start
        assert feed.warmups == 1


@pytest.mark.asyncio
async def test_readiness_probe_answers_while_an_exchange_hangs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify health checks still answer, so a slow venue is not read as death."""
    app, _supervisor, feed = _build(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        bot_id = (await ac.post("/api/bots", json=_BOT_PAYLOAD, headers=_auth())).json()["id"]
        start = asyncio.create_task(ac.post(f"/api/bots/{bot_id}/start", headers=_auth()))
        await _await_entry(feed)

        assert await _get_while_blocked(ac, "/healthz", feed) == 200

        await start


@pytest.mark.asyncio
async def test_a_slow_bot_does_not_delay_another_bots_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify one bot stuck on a slow venue does not hold up a healthy one."""
    slow_feed = _BlockingCandleFeed(seconds=BLOCK_SECONDS)
    hubs = {"slow": _hub_with(slow_feed), "fast": _hub_with(_BlockingCandleFeed(seconds=0.0))}

    monkeypatch.setattr("tradingbot.service.supervisor.build_venue", lambda *a, **k: _BlockingVenue(0.0))
    monkeypatch.setattr("tradingbot.service.supervisor.build_strategy", lambda *a, **k: _IdleStrategy())
    supervisor = BotSupervisor(
        # ETH is the "fast" market; everything else is the stuck one.
        hub_factory=lambda cfg: hubs["fast"] if cfg.symbol.startswith("ETH") else hubs["slow"],
        event_bus=EventBus(),
        exposure=ExposureTracker(),
    )
    app = create_app(store=_store(tmp_path), supervisor=supervisor)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        slow_id = (await ac.post("/api/bots", json=_BOT_PAYLOAD, headers=_auth())).json()["id"]
        fast_id = (
            await ac.post(
                "/api/bots", json={**_BOT_PAYLOAD, "symbol": "ETH/USD"}, headers=_auth()
            )
        ).json()["id"]

        slow = asyncio.create_task(ac.post(f"/api/bots/{slow_id}/start", headers=_auth()))
        await _await_entry(slow_feed)

        response = await ac.post(f"/api/bots/{fast_id}/start", headers=_auth())
        done_at = time.monotonic()

        assert response.status_code == 200
        exited_at = slow_feed.exited_at
        assert exited_at is None or done_at < exited_at, (
            "the healthy bot only started after the stuck one finished"
        )
        await slow
        await ac.post(f"/api/bots/{fast_id}/stop", headers=_auth())
        await ac.post(f"/api/bots/{slow_id}/stop", headers=_auth())


@pytest.mark.asyncio
async def test_position_polling_does_not_block_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the periodic position/PnL refresh runs off the event loop."""
    _venue = _BlockingVenue(BLOCK_SECONDS)
    app, _supervisor, _feed = _build(
        tmp_path, monkeypatch, feed=_BlockingCandleFeed(seconds=0.0), venue=_venue
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        bot_id = (await ac.post("/api/bots", json=_BOT_PAYLOAD, headers=_auth())).json()["id"]
        await ac.post(f"/api/bots/{bot_id}/start", headers=_auth())
        venue = _venue
        await _await_entry(venue)  # the 50ms poll fires into the blocking venue

        assert await _get_while_blocked(ac, "/api/bots", venue) == 200
        await ac.post(f"/api/bots/{bot_id}/stop", headers=_auth())


class _PushStream:
    """Stream feed a test can drive, pushing one bar into the running bot."""

    def __init__(self) -> None:
        self._handlers: dict[str, object] = {}
        self._handler = None
        self._stopped = asyncio.Event()

    def on_bar(self, handler) -> None:
        self._handler = handler

    def on_bar_for(self, symbol: str, handler) -> None:
        self._handlers[symbol] = handler

    async def run_async(self, *symbols: str) -> None:
        await self._stopped.wait()

    def stop(self) -> None:
        self._stopped.set()

    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        del symbol, timeframe, limit
        return []

    def push(self, symbol: str, candle: Candle) -> None:
        handler = self._handlers.get(symbol) or self._handler
        assert handler is not None, "nothing subscribed to the stream"
        handler(candle)  # type: ignore[operator]


@pytest.mark.asyncio
async def test_order_placement_does_not_block_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify a bar that triggers a slow order does not freeze the API.

    This is the deepest path in the issue: stream callback -> strategy ->
    router -> venue.place_order, all synchronous.
    """
    venue = _BlockingVenue(BLOCK_SECONDS)
    stream = _PushStream()
    hub = MarketDataHub(
        stream_feed=cast(Any, stream),
        candle_feed=cast(Any, _BlockingCandleFeed(seconds=0.0)),
        limiter=RateLimiter(1000, 1000),
    )
    monkeypatch.setattr("tradingbot.service.supervisor.build_venue", lambda *a, **k: venue)
    monkeypatch.setattr("tradingbot.service.supervisor.build_strategy", lambda *a, **k: _SignalStrategy())
    supervisor = BotSupervisor(
        hub_factory=lambda cfg: hub,
        event_bus=EventBus(),
        exposure=ExposureTracker(),
    )
    app = create_app(store=_store(tmp_path), supervisor=supervisor)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        bot_id = (await ac.post("/api/bots", json=_BOT_PAYLOAD, headers=_auth())).json()["id"]
        await ac.post(f"/api/bots/{bot_id}/start", headers=_auth())
        await asyncio.sleep(0.05)

        # A closed bar arrives; the strategy signals, the router places an
        # order, and the venue hangs.
        stream.push("BTC/USD", _candle(ts=2, close=101.0))
        await _await_entry(venue)

        assert await _get_while_blocked(ac, "/api/bots", venue) == 200

        await ac.post(f"/api/bots/{bot_id}/stop", headers=_auth())
