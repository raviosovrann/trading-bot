"""Tests for the CCXT streaming candle feed."""

from typing import Any

from tradingbot.models import Candle
from tradingbot.stream import CcxtStreamFeed


def _row(ts, close=1.0):
    # ccxt OHLCV row: [ts_ms, open, high, low, close, volume]
    return [ts, close, close, close, close, 1.0]


class _FakeProExchange:
    """Async ccxt.pro-like stub. Delivers pre-seeded watch_ohlcv batches, then
    flips the feed's stop flag so the run loop exits deterministically."""

    def __init__(self, batches):
        self._batches = list(batches)
        self.closed = False
        self.watch_calls = []
        self.feed: Any = None  # wired after feed construction

    async def watch_ohlcv(self, symbol, timeframe):
        self.watch_calls.append((symbol, timeframe))
        batch = self._batches.pop(0)
        if not self._batches and self.feed is not None:
            self.feed._stopped = True  # stop after this batch is processed
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
    assert feed._stopped is True
