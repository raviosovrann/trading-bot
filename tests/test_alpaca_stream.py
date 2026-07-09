from datetime import datetime, timezone

import pytest

from tradingbot.models import Candle
from tradingbot.stream import AlpacaStreamFeed


class _Bar:
    def __init__(self, ts, o, h, l, c, v):
        self.timestamp = ts
        self.open = o
        self.high = h
        self.low = l
        self.close = c
        self.volume = v


class _FakeStreamClient:
    """Records subscribe/run/stop calls; never touches the network."""

    def __init__(self):
        self.subscribed = []
        self.run_called = 0
        self.stop_called = 0

    def subscribe_bars(self, handler, *symbols):
        self.subscribed.append((handler, symbols))

    def run(self):
        self.run_called += 1

    def stop(self):
        self.stop_called += 1


class _FakeWarmupFeed:
    """Records warmup_candles calls and returns canned candles."""

    def __init__(self, candles=None):
        self.calls = []
        self._candles = candles or []

    def warmup_candles(self, symbol, timeframe, limit):
        self.calls.append((symbol, timeframe, limit))
        return list(self._candles)


def _bar(ts_dt, o=100, h=110, l=90, c=105, v=10):
    return _Bar(ts_dt, o, h, l, c, v)


def _make_feed(client=None, warmup_feed=None):
    return AlpacaStreamFeed(
        client=client or _FakeStreamClient(),
        warmup_feed=warmup_feed or _FakeWarmupFeed(),
    )


def test_bar_pushed_invokes_registered_handler():
    feed = _make_feed()
    received = []
    feed.on_bar(received.append)

    feed._on_ws_bar(_bar(datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)))

    assert len(received) == 1
    assert isinstance(received[0], Candle)


def test_duplicate_timestamp_bar_not_re_emitted():
    feed = _make_feed()
    received = []
    feed.on_bar(received.append)

    ts = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    feed._on_ws_bar(_bar(ts))
    feed._on_ws_bar(_bar(ts))

    assert len(received) == 1


def test_warmup_fetches_history_via_rest_not_stream():
    client = _FakeStreamClient()
    warmup = _FakeWarmupFeed(candles=[Candle(timestamp=1, open=1, high=1, low=1, close=1, volume=1)])
    feed = AlpacaStreamFeed(client=client, warmup_feed=warmup)

    candles = feed.warmup_candles("BTC/USD", "5Min", 3)

    assert warmup.calls == [("BTC/USD", "5Min", 3)]
    assert candles == warmup._candles
    # The stream client must not be used for warmup.
    assert client.subscribed == []
    assert client.run_called == 0


def test_on_bar_registers_handler_before_run():
    feed = _make_feed()
    seen = []
    feed.on_bar(lambda c: seen.append(("first", c)))

    feed._on_ws_bar(_bar(datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)))

    assert len(seen) == 1
    assert seen[0][0] == "first"


def test_stop_closes_stream_cleanly():
    client = _FakeStreamClient()
    feed = _make_feed(client=client)

    feed.stop()

    assert client.stop_called == 1


def test_handler_receives_normalized_candle():
    feed = _make_feed()
    received = []
    feed.on_bar(received.append)

    ts = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    feed._on_ws_bar(_bar(ts, o=100, h=120, l=95, c=110, v=42))

    candle = received[0]
    assert isinstance(candle, Candle)
    assert candle.timestamp == int(ts.timestamp() * 1000)
    assert candle.open == 100.0
    assert candle.high == 120.0
    assert candle.low == 95.0
    assert candle.close == 110.0
    assert candle.volume == 42.0


def test_requires_client_or_credentials():
    with pytest.raises(ValueError):
        AlpacaStreamFeed(client=None, warmup_feed=_FakeWarmupFeed())
