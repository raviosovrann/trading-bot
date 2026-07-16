"""Tests for the Tradovate market-data feeds (injected client, no network)."""

from typing import Any

from tradingbot.models import Candle
from tradingbot.tradovate_feed import TradovateCandleFeed, TradovateStreamFeed


def _row(ts, close=1.0):
    return [ts, close, close, close, close, 1.0]


class _FakeCandleClient:
    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    def get_chart(self, symbol, timeframe, limit):
        self.calls.append((symbol, timeframe, limit))
        return list(self._rows)


class _FakeStreamClient:
    def __init__(self, batches):
        self._batches = list(batches)
        self.closed = False
        self.feed: Any = None

    async def watch_chart(self, symbol, timeframe):
        batch = self._batches.pop(0)
        if not self._batches and self.feed is not None:
            self.feed._stopped = True
        return batch

    async def close(self):
        self.closed = True


# --- candle feed ----------------------------------------------------------- #

def test_candle_construct_requires_client():
    import pytest
    with pytest.raises(ValueError):
        TradovateCandleFeed(None)


def test_warmup_drops_forming_bar():
    rows = [_row(1000), _row(2000), _row(3000), _row(4000)]  # last forming
    feed = TradovateCandleFeed(_FakeCandleClient(rows))
    candles = feed.warmup_candles("MBTF6", "5m", 3)
    assert [c.timestamp for c in candles] == [1000, 2000, 3000]
    assert all(isinstance(c, Candle) for c in candles)


def test_warmup_zero_limit_returns_empty():
    client = _FakeCandleClient([_row(1000)])
    assert TradovateCandleFeed(client).warmup_candles("MBTF6", "5m", 0) == []
    assert client.calls == []


def test_latest_closed_skips_forming():
    feed = TradovateCandleFeed(_FakeCandleClient([_row(1000), _row(2000)]))
    candle = feed.latest_closed_candle("MBTF6", "1h")
    assert candle is not None and candle.timestamp == 1000


def test_latest_closed_empty_returns_none():
    assert TradovateCandleFeed(_FakeCandleClient([])).latest_closed_candle("MBTF6", "5m") is None


# --- stream feed ----------------------------------------------------------- #

def test_stream_construct_requires_client():
    import pytest
    with pytest.raises(ValueError):
        TradovateStreamFeed(client=None)


def _stream(batches):
    client = _FakeStreamClient(batches)
    feed = TradovateStreamFeed(client=client, timeframe="5m")
    client.feed = feed
    received = []
    feed.on_bar(lambda c: received.append(c))
    return feed, client, received


def test_run_emits_closed_bar_and_closes_client():
    feed, client, received = _stream([[_row(1000), _row(2000)]])
    feed.run("MBTF6")
    assert [c.timestamp for c in received] == [1000]
    assert client.closed is True


def test_run_dedups_forming_bar_across_batches():
    feed, client, received = _stream([
        [_row(1000), _row(2000)],
        [_row(1000), _row(2000), _row(3000)],
    ])
    feed.run("MBTF6")
    assert [c.timestamp for c in received] == [1000, 2000]


def test_stop_is_idempotent():
    feed, _, _ = _stream([[_row(1000)]])
    feed.stop()
    feed.stop()
    assert feed._stopped is True


def test_warmup_delegates_to_warmup_feed():
    warmup = _FakeCandleClient([_row(1000), _row(2000)])
    feed = TradovateStreamFeed(client=_FakeStreamClient([[_row(1000)]]),
                               warmup_feed=TradovateCandleFeed(warmup), timeframe="5m")
    out = feed.warmup_candles("MBTF6", "5m", 1)
    assert len(out) == 1 and out[0].timestamp == 1000
