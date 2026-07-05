from datetime import datetime, timezone

import pytest

from tradingbot.config import load_config
from tradingbot.datafeed import (
    AlpacaCandleFeed,
    InMemoryCandleFeed,
    _parse_timeframe,
    build_feed,
)
from tradingbot.models import Candle


class _Bar:
    def __init__(self, ts, o, h, l, c, v):
        self.timestamp = ts
        self.open = o
        self.high = h
        self.low = l
        self.close = c
        self.volume = v


class _FakeCryptoClient:
    def __init__(self, bars):
        self._bars = bars
        self.last_request = None

    def get_crypto_bars(self, request):
        self.last_request = request
        return {"BTC/USD": self._bars}


def _make_bars():
    return [
        _Bar(datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc), 100, 110, 90, 105, 10),
        _Bar(datetime(2024, 1, 1, 0, 5, tzinfo=timezone.utc), 105, 115, 95, 110, 20),
        _Bar(datetime(2024, 1, 1, 0, 10, tzinfo=timezone.utc), 110, 120, 100, 115, 30),
    ]


def test_warmup_candles_normalizes_bars_oldest_first():
    bars = _make_bars()
    client = _FakeCryptoClient(bars)
    feed = AlpacaCandleFeed(client)

    candles = feed.warmup_candles("BTC/USD", "5Min", 3)

    assert len(candles) == 3
    assert all(isinstance(c, Candle) for c in candles)
    assert [c.timestamp for c in candles] == [
        int(bars[0].timestamp.timestamp() * 1000),
        int(bars[1].timestamp.timestamp() * 1000),
        int(bars[2].timestamp.timestamp() * 1000),
    ]
    assert candles[0].open == 100.0
    assert candles[0].high == 110.0
    assert candles[0].low == 90.0
    assert candles[0].close == 105.0
    assert candles[0].volume == 10.0


def test_warmup_candles_truncates_to_limit():
    bars = _make_bars()
    client = _FakeCryptoClient(bars)
    feed = AlpacaCandleFeed(client)

    candles = feed.warmup_candles("BTC/USD", "5Min", 2)

    assert len(candles) == 2
    assert [c.timestamp for c in candles] == [
        int(bars[1].timestamp.timestamp() * 1000),
        int(bars[2].timestamp.timestamp() * 1000),
    ]


def test_warmup_candles_returns_empty_for_non_positive_limit():
    client = _FakeCryptoClient(_make_bars())
    feed = AlpacaCandleFeed(client)

    assert feed.warmup_candles("BTC/USD", "5Min", 0) == []
    assert feed.warmup_candles("BTC/USD", "5Min", -1) == []


def test_latest_closed_candle_returns_most_recent():
    bars = _make_bars()
    client = _FakeCryptoClient(bars)
    feed = AlpacaCandleFeed(client)

    latest = feed.latest_closed_candle("BTC/USD", "5Min")

    assert latest is not None
    assert latest.timestamp == int(bars[-1].timestamp.timestamp() * 1000)
    assert latest.close == 115.0


def test_latest_closed_candle_returns_none_when_empty():
    client = _FakeCryptoClient([])
    feed = AlpacaCandleFeed(client)

    assert feed.latest_closed_candle("BTC/USD", "5Min") is None


def test_alpaca_candle_feed_requires_client():
    with pytest.raises(ValueError):
        AlpacaCandleFeed(None)


def test_parse_timeframe_minute():
    from alpaca.data.timeframe import TimeFrameUnit

    tf = _parse_timeframe("5Min")
    assert tf.amount_value == 5
    assert tf.unit_value == TimeFrameUnit.Minute


def test_parse_timeframe_hour():
    from alpaca.data.timeframe import TimeFrameUnit

    tf = _parse_timeframe("1Hour")
    assert tf.amount_value == 1
    assert tf.unit_value == TimeFrameUnit.Hour


def test_parse_timeframe_day():
    from alpaca.data.timeframe import TimeFrameUnit

    tf = _parse_timeframe("1Day")
    assert tf.amount_value == 1
    assert tf.unit_value == TimeFrameUnit.Day


def test_parse_timeframe_invalid_raises():
    with pytest.raises(ValueError):
        _parse_timeframe("bogus")


def test_build_feed_fake_venue():
    cfg = load_config({"VENUE": "fake"})
    feed = build_feed(cfg)
    assert isinstance(feed, InMemoryCandleFeed)



def test_build_feed_alpaca_builds_real_feed():
    cfg = load_config(
        {"VENUE": "alpaca", "ALPACA_API_KEY": "key", "ALPACA_API_SECRET": "secret"}
    )
    feed = build_feed(cfg)
    assert isinstance(feed, AlpacaCandleFeed)
