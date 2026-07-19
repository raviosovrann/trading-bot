"""Tests for the CCXT streaming candle feed."""

import asyncio
from typing import Any

import pytest

from tradingbot.models import Candle
from tradingbot.stream import CcxtStreamFeed, StreamingNotSupported


def _row(ts, close=1.0):
    # ccxt OHLCV row: [ts_ms, open, high, low, close, volume]
    return [ts, close, close, close, close, 1.0]


class _FakeProExchange:
    """Async ccxt.pro-like stub. Delivers pre-seeded watch_ohlcv batches, then
    stops the feed so the run loop exits deterministically."""

    def __init__(self, batches):
        self._batches = list(batches)
        self.closed = False
        self.watch_calls = []
        self.feed: Any = None  # wired after feed construction

    async def watch_ohlcv(self, symbol, timeframe):
        self.watch_calls.append((symbol, timeframe))
        batch = self._batches.pop(0)
        if not self._batches and self.feed is not None:
            self.feed.stop()  # stop after this batch is processed
        return batch

    async def close(self):
        self.closed = True


class _WarmupStub:
    def __init__(self):
        self.calls = []

    def warmup_candles(self, symbol, timeframe, limit):
        self.calls.append((symbol, timeframe, limit))
        return [Candle(timestamp=1, open=1, high=1, low=1, close=1, volume=1)]


def _make(batches):
    ex = _FakeProExchange(batches)
    feed = CcxtStreamFeed(exchange=ex, warmup_feed=_WarmupStub(), timeframe="5m")
    ex.feed = feed
    received = []
    feed.on_bar(lambda c: received.append(c))
    return feed, ex, received


def test_construct_requires_exchange():
    """Verify that CcxtStreamFeed requires an exchange."""
    import pytest

    with pytest.raises(ValueError):
        CcxtStreamFeed(exchange=None)


def test_warmup_delegates_to_warmup_feed():
    """Verify that warmup delegates to the provided warmup feed."""
    warmup = _WarmupStub()
    ex = _FakeProExchange([[_row(1000)]])
    feed = CcxtStreamFeed(exchange=ex, warmup_feed=warmup, timeframe="5m")
    out = feed.warmup_candles("BTC/USD", "5m", 20)
    assert len(out) == 1
    assert warmup.calls == [("BTC/USD", "5m", 20)]


def test_run_emits_closed_bar_and_closes_exchange():
    """Verify that the run loop emits only closed bars and closes the exchange."""
    # one batch: [closed@1000, forming@2000] -> only 1000 emitted
    feed, ex, received = _make([[_row(1000), _row(2000)]])
    feed.run("BTC/USD")
    assert [c.timestamp for c in received] == [1000]
    assert ex.watch_calls == [("BTC/USD", "5m")]
    assert ex.closed is True  # loop finally closed the async exchange


def test_run_dedups_forming_candle_across_batches():
    """Verify that the run loop deduplicates forming candles across batches."""
    # batch1: closed 1000, forming 2000 ; batch2: closed 1000+2000, forming 3000
    feed, ex, received = _make([
        [_row(1000), _row(2000)],
        [_row(1000), _row(2000), _row(3000)],
    ])
    feed.run("BTC/USD")
    # 1000 then 2000, each once; 3000 still forming -> not emitted
    assert [c.timestamp for c in received] == [1000, 2000]


def test_stop_is_idempotent():
    """Verify that stop can be called multiple times safely."""
    feed, ex, _ = _make([[_row(1000)]])
    feed.stop()
    feed.stop()  # second call must not raise
    assert feed._should_run("BTC/USD") is False


class _MultiSymbolExchange:
    """Async ccxt.pro-like stub modelling real client close semantics.

    A real exchange client is shared by every symbol watch and cannot be used
    after ``close()``. Modelling that is the whole point: the previous fakes
    tolerated post-close use, which hid the bug where one symbol unsubscribing
    tore down the connection for the others.
    """

    def __init__(self) -> None:
        self.closed = False
        self.close_calls = 0
        self.pending: dict[str, list] = {}
        self._gates: dict[str, asyncio.Event] = {}

    def gate(self, symbol: str) -> asyncio.Event:
        return self._gates.setdefault(symbol, asyncio.Event())

    def deliver(self, symbol: str, rows: list) -> None:
        self.pending.setdefault(symbol, []).append(rows)
        self.gate(symbol).set()

    async def watch_ohlcv(self, symbol, timeframe):
        del timeframe
        if self.closed:
            raise RuntimeError("exchange is closed")
        gate = self.gate(symbol)
        await gate.wait()
        queued = self.pending.get(symbol) or []
        if queued:
            return queued.pop(0)
        gate.clear()
        await gate.wait()
        return (self.pending.get(symbol) or [[]]).pop(0)

    async def close(self):
        self.closed = True
        self.close_calls += 1


@pytest.mark.asyncio
async def test_stopping_one_symbol_leaves_the_other_streaming() -> None:
    """Unsubscribing one symbol must not close the shared client."""
    exchange = _MultiSymbolExchange()
    feed = CcxtStreamFeed(exchange=exchange, timeframe="1m")
    btc: list[Candle] = []
    eth: list[Candle] = []
    feed.on_bar_for("BTC/USD", btc.append)
    feed.on_bar_for("ETH/USD", eth.append)

    btc_task = asyncio.create_task(feed.run_async("BTC/USD"))
    eth_task = asyncio.create_task(feed.run_async("ETH/USD"))
    await asyncio.sleep(0)

    feed.stop_symbol("BTC/USD")
    exchange.deliver("BTC/USD", [_row(1), _row(2)])
    await asyncio.wait_for(btc_task, timeout=1.0)

    assert not exchange.closed, "closing the client killed the other symbol's stream"

    # The surviving symbol still receives data.
    exchange.deliver("ETH/USD", [_row(1, 5.0), _row(2, 6.0)])
    await asyncio.sleep(0.01)
    assert eth, "second symbol stopped receiving candles"

    feed.stop_symbol("ETH/USD")
    exchange.deliver("ETH/USD", [_row(3), _row(4)])
    await asyncio.wait_for(eth_task, timeout=1.0)
    assert exchange.closed, "client not closed once the last symbol stopped"


@pytest.mark.asyncio
async def test_feed_can_be_restarted_after_a_full_stop() -> None:
    """A cached hub whose bots all stopped must be usable again later."""
    exchanges: list[_MultiSymbolExchange] = []

    def build() -> _MultiSymbolExchange:
        exchange = _MultiSymbolExchange()
        exchanges.append(exchange)
        return exchange

    feed = CcxtStreamFeed(exchange=build(), exchange_factory=build, timeframe="1m")
    received: list[Candle] = []
    feed.on_bar_for("BTC/USD", received.append)

    first = asyncio.create_task(feed.run_async("BTC/USD"))
    await asyncio.sleep(0)
    feed.stop()
    exchanges[0].deliver("BTC/USD", [_row(1), _row(2)])
    await asyncio.wait_for(first, timeout=1.0)
    assert exchanges[0].closed

    # Restart: a closed client cannot be reused, so a fresh one is built.
    second = asyncio.create_task(feed.run_async("BTC/USD"))
    await asyncio.sleep(0)
    assert len(exchanges) == 2, "restart reused the closed client"
    exchanges[1].deliver("BTC/USD", [_row(10, 9.0), _row(11, 9.0)])
    await asyncio.sleep(0.01)

    assert any(c.close == 9.0 for c in received), "restarted feed delivered no candles"
    feed.stop()
    exchanges[1].deliver("BTC/USD", [_row(12), _row(13)])
    await asyncio.wait_for(second, timeout=1.0)


@pytest.mark.asyncio
async def test_stop_only_affects_the_named_symbol() -> None:
    """``stop_symbol`` is per-symbol; the global ``stop`` ends everything."""
    exchange = _MultiSymbolExchange()
    feed = CcxtStreamFeed(exchange=exchange, timeframe="1m")
    feed.on_bar_for("BTC/USD", lambda c: None)
    feed.on_bar_for("ETH/USD", lambda c: None)
    btc_task = asyncio.create_task(feed.run_async("BTC/USD"))
    eth_task = asyncio.create_task(feed.run_async("ETH/USD"))
    await asyncio.sleep(0)

    feed.stop()
    exchange.deliver("BTC/USD", [_row(1), _row(2)])
    exchange.deliver("ETH/USD", [_row(1), _row(2)])
    await asyncio.wait_for(asyncio.gather(btc_task, eth_task), timeout=1.0)

    assert exchange.closed
    assert exchange.close_calls == 1, "client closed more than once"


class _NoWatchExchange:
    """ccxt.pro-like client for a venue that cannot stream candles."""

    def __init__(self) -> None:
        self.has = {"watchOHLCV": False, "watchTrades": True, "fetchOHLCV": True}
        self.id = "coinbase"


class _CanWatchExchange:
    def __init__(self) -> None:
        self.has = {"watchOHLCV": True}
        self.id = "binance"


def test_construction_rejects_a_venue_that_cannot_stream_candles():
    """A venue without watchOHLCV can never deliver bars — refuse it up front.

    Coinbase reports watchOHLCV=False, so a bot built on it warms up over REST,
    starts, and then silently never sees a bar (#170).
    """
    with pytest.raises(StreamingNotSupported) as excinfo:
        CcxtStreamFeed(exchange=_NoWatchExchange(), warmup_feed=_WarmupStub())

    message = str(excinfo.value)
    assert "coinbase" in message
    assert "watchOHLCV" in message
    # The operator needs to know a restart will not help.
    assert "not" in message.lower()


def test_construction_accepts_a_venue_that_can_stream():
    """A capable venue is unaffected."""
    feed = CcxtStreamFeed(exchange=_CanWatchExchange(), warmup_feed=_WarmupStub())
    assert feed is not None


def test_construction_allows_a_client_that_does_not_declare_capabilities():
    """Test doubles and non-ccxt clients without ``has`` still work."""
    feed = CcxtStreamFeed(exchange=_FakeProExchange([[]]), warmup_feed=_WarmupStub())
    assert feed is not None
