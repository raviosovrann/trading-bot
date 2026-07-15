from __future__ import annotations

"""Tests for the market data hub."""

import asyncio
from collections.abc import Callable

import pytest

from tradingbot.models import Candle
from tradingbot.service.datahub import MarketDataHub
from tradingbot.service.ratelimit import RateLimiter


def _c(timestamp: int) -> Candle:
    return Candle(
        timestamp=timestamp,
        open=1.0,
        high=1.0,
        low=1.0,
        close=1.0,
        volume=1.0,
    )


class _FakeCandleFeed:
    def __init__(self) -> None:
        self.calls = 0

    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        del symbol, timeframe, limit
        self.calls += 1
        return [_c(1)]

    def latest_closed_candle(self, symbol: str, timeframe: str) -> Candle:
        del symbol, timeframe
        return _c(1)


class _FakeStream:
    def __init__(self) -> None:
        self.run_calls: list[tuple[str, ...]] = []
        self.stop_calls = 0
        self._handler: Callable[[Candle], None] | None = None
        self._handlers: dict[str, Callable[[Candle], None]] = {}
        self._stopped = asyncio.Event()

    def warmup_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        del symbol, timeframe, limit
        return []

    def run(self, *symbols: str) -> None:
        del symbols

    def on_bar(self, handler: Callable[[Candle], None]) -> None:
        self._handler = handler

    def on_bar_for(self, symbol: str, handler: Callable[[Candle], None]) -> None:
        self._handlers[symbol] = handler

    async def run_async(self, *symbols: str) -> None:
        self.run_calls.append(symbols)
        await self._stopped.wait()

    def stop(self) -> None:
        self.stop_calls += 1
        self._stopped.set()

    def emit(self, symbol: str, candle: Candle) -> None:
        handler = self._handlers.get(symbol, self._handler)
        assert handler is not None
        handler(candle)


@pytest.mark.asyncio
async def test_warmup_deduped_and_cached() -> None:
    """Verify that warmup requests are deduplicated and cached."""
    feed = _FakeCandleFeed()
    hub = MarketDataHub(
        stream_feed=_FakeStream(),
        candle_feed=feed,
        limiter=RateLimiter(1000, 1000),
        mtf_cache_seconds=60.0,
        clock=lambda: 0.0,
    )

    first = await hub.warmup("BTC/USD", "1h", 10)
    second = await hub.warmup("BTC/USD", "1h", 10)

    assert feed.calls == 1
    assert first == second


@pytest.mark.asyncio
async def test_identical_subscribers_share_stream_and_fan_out_candles() -> None:
    """Verify that identical subscribers share a stream and receive fanned-out candles."""
    stream = _FakeStream()
    hub = MarketDataHub(
        stream_feed=stream,
        candle_feed=_FakeCandleFeed(),
        limiter=RateLimiter(1000, 1000),
    )
    first: list[Candle] = []
    second: list[Candle] = []

    hub.subscribe("BTC/USD", "1m", first.append)
    hub.subscribe("BTC/USD", "1m", second.append)
    await asyncio.sleep(0)

    assert stream.run_calls == [("BTC/USD",)]
    stream.emit("BTC/USD", _c(2))
    assert first == [_c(2)]
    assert second == [_c(2)]
    assert hub.latest_price("BTC/USD", "1m") == 1.0

    hub.unsubscribe("BTC/USD", "1m", first.append)
    assert stream.stop_calls == 0
    hub.unsubscribe("BTC/USD", "1m", second.append)
    assert stream.stop_calls == 1


@pytest.mark.asyncio
async def test_different_subscriptions_keep_stream_routing_isolated() -> None:
    """Verify that different subscriptions keep their stream routing isolated."""
    stream = _FakeStream()
    hub = MarketDataHub(
        stream_feed=stream,
        candle_feed=_FakeCandleFeed(),
        limiter=RateLimiter(1000, 1000),
    )
    btc: list[Candle] = []
    eth: list[Candle] = []

    hub.subscribe("BTC/USD", "1m", btc.append)
    hub.subscribe("ETH/USD", "1m", eth.append)
    await asyncio.sleep(0)

    assert set(stream.run_calls) == {("BTC/USD",), ("ETH/USD",)}
    stream.emit("BTC/USD", _c(2))
    stream.emit("ETH/USD", _c(3))

    assert btc == [_c(2)]
    assert eth == [_c(3)]

    hub.unsubscribe("BTC/USD", "1m", btc.append)
    hub.unsubscribe("ETH/USD", "1m", eth.append)


@pytest.mark.asyncio
async def test_duplicate_subscribe_is_ignored() -> None:
    """Verify that subscribing the same handler twice only starts one stream."""
    stream = _FakeStream()
    hub = MarketDataHub(
        stream_feed=stream,
        candle_feed=_FakeCandleFeed(),
        limiter=RateLimiter(1000, 1000),
    )
    received: list[Candle] = []

    hub.subscribe("BTC/USD", "1m", received.append)
    hub.subscribe("BTC/USD", "1m", received.append)
    await asyncio.sleep(0)

    assert stream.run_calls == [("BTC/USD",)]
    stream.emit("BTC/USD", _c(2))
    assert received == [_c(2)]
    hub.unsubscribe("BTC/USD", "1m", received.append)


@pytest.mark.asyncio
async def test_unsubscribe_unknown_handler_is_silent() -> None:
    """Verify that unsubscribing a handler that was never subscribed is a no-op."""
    hub = MarketDataHub(
        stream_feed=_FakeStream(),
        candle_feed=_FakeCandleFeed(),
        limiter=RateLimiter(1000, 1000),
    )
    hub.unsubscribe("BTC/USD", "1m", print)


def test_negative_cache_seconds_rejected() -> None:
    """Verify that a negative cache TTL is rejected."""
    with pytest.raises(ValueError):
        MarketDataHub(
            stream_feed=_FakeStream(),
            candle_feed=_FakeCandleFeed(),
            limiter=RateLimiter(1000, 1000),
            mtf_cache_seconds=-1.0,
        )


@pytest.mark.asyncio
async def test_non_keyed_stream_with_multiple_symbols_raises() -> None:
    """Verify that multi-symbol subscriptions require keyed stream handlers."""
    class _NonKeyedStream(_FakeStream):
        def on_bar_for(self, symbol: str, handler: Callable[[Candle], None]) -> None:
            del symbol, handler
            raise AttributeError

    stream = _NonKeyedStream()
    stream.on_bar_for = None  # type: ignore[assignment]
    hub = MarketDataHub(
        stream_feed=stream,
        candle_feed=_FakeCandleFeed(),
        limiter=RateLimiter(1000, 1000),
    )
    first_handler = print
    hub.subscribe("BTC/USD", "1m", first_handler)
    with pytest.raises(RuntimeError):
        hub.subscribe("ETH/USD", "1m", print)
    hub.unsubscribe("BTC/USD", "1m", first_handler)


@pytest.mark.asyncio
async def test_handler_exception_does_not_break_other_handlers() -> None:
    """Verify that a failing handler does not stop other handlers from receiving."""
    stream = _FakeStream()
    hub = MarketDataHub(
        stream_feed=stream,
        candle_feed=_FakeCandleFeed(),
        limiter=RateLimiter(1000, 1000),
    )
    good: list[Candle] = []

    def _bad(_: Candle) -> None:
        raise RuntimeError("boom")

    hub.subscribe("BTC/USD", "1m", _bad)
    hub.subscribe("BTC/USD", "1m", good.append)
    await asyncio.sleep(0)

    stream.emit("BTC/USD", _c(2))
    assert good == [_c(2)]
    hub.unsubscribe("BTC/USD", "1m", _bad)
    hub.unsubscribe("BTC/USD", "1m", good.append)


@pytest.mark.asyncio
async def test_concurrent_warmup_dedupes_single_fetch() -> None:
    """Verify concurrent warmup calls for the same key only fetch once."""
    feed = _FakeCandleFeed()
    hub = MarketDataHub(
        stream_feed=_FakeStream(),
        candle_feed=feed,
        limiter=RateLimiter(1000, 1000),
        mtf_cache_seconds=60.0,
        clock=lambda: 0.0,
    )
    results = await asyncio.gather(
        hub.warmup("BTC/USD", "1h", 10),
        hub.warmup("BTC/USD", "1h", 10),
    )
    assert feed.calls == 1
    assert results[0] == results[1]
