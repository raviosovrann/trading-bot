import pytest

from tradingbot.config import load_config
from tradingbot.datafeed import (
    CoinbaseCandleFeed,
    _coinbase_granularity,
    build_feed,
)
from tradingbot.models import Candle


class _FakeCoinbaseResponse:
    def __init__(self, candles):
        self.candles = candles


class _CoinbaseCandle:
    def __init__(self, start, o, h, l, c, v):
        self.start = str(start)  # Coinbase API returns start as string
        self.open = str(o)
        self.high = str(h)
        self.low = str(l)
        self.close = str(c)
        self.volume = str(v)


class _FakeCoinbaseClient:
    def __init__(self, candles):
        self._candles = candles
        self.last_kwargs = {}

    def get_candles(self, product_id, **kwargs):
        self.last_kwargs = kwargs
        return _FakeCoinbaseResponse(self._candles)


def _make_candles():
    # Three candles with Unix timestamps around 2024-01-01
    return [
        _CoinbaseCandle(1704067200, 100, 110, 90, 105, 10),   # 2024-01-01 00:00:00 UTC
        _CoinbaseCandle(1704067500, 105, 115, 95, 110, 20),   # +5 min
        _CoinbaseCandle(1704067800, 110, 120, 100, 115, 30),  # +10 min
    ]


def test_warmup_candles_normalizes_candles_oldest_first():
    candles = _make_candles()
    client = _FakeCoinbaseClient(candles)
    feed = CoinbaseCandleFeed(client)

    result = feed.warmup_candles("BTC/USD", "5Min", 3)

    assert len(result) == 3
    assert all(isinstance(c, Candle) for c in result)
    assert [c.timestamp for c in result] == [
        1704067200 * 1000,
        1704067500 * 1000,
        1704067800 * 1000,
    ]
    assert result[0].open == 100.0
    assert result[0].high == 110.0
    assert result[0].low == 90.0
    assert result[0].close == 105.0
    assert result[0].volume == 10.0


def test_warmup_candles_truncates_to_limit():
    candles = _make_candles()
    client = _FakeCoinbaseClient(candles)
    feed = CoinbaseCandleFeed(client)

    result = feed.warmup_candles("BTC/USD", "5Min", 2)

    assert len(result) == 2
    assert [c.timestamp for c in result] == [
        1704067500 * 1000,
        1704067800 * 1000,
    ]


def test_warmup_candles_returns_empty_for_non_positive_limit():
    client = _FakeCoinbaseClient(_make_candles())
    feed = CoinbaseCandleFeed(client)

    assert feed.warmup_candles("BTC/USD", "5Min", 0) == []
    assert feed.warmup_candles("BTC/USD", "5Min", -1) == []


def test_latest_closed_candle_returns_most_recent():
    candles = _make_candles()
    client = _FakeCoinbaseClient(candles)
    feed = CoinbaseCandleFeed(client)

    latest = feed.latest_closed_candle("BTC/USD", "5Min")

    assert latest is not None
    assert latest.timestamp == 1704067800 * 1000
    assert latest.close == 115.0


def test_latest_closed_candle_returns_none_when_empty():
    client = _FakeCoinbaseClient([])
    feed = CoinbaseCandleFeed(client)

    assert feed.latest_closed_candle("BTC/USD", "5Min") is None


def test_coinbase_candle_feed_requires_client():
    with pytest.raises(ValueError):
        CoinbaseCandleFeed(None)


def test_coinbase_granularity_minute():
    assert _coinbase_granularity("5Min") == "FIVE_MINUTE"


def test_coinbase_granularity_hour():
    assert _coinbase_granularity("1Hour") == "ONE_HOUR"


def test_coinbase_granularity_day():
    assert _coinbase_granularity("1Day") == "ONE_DAY"


def test_coinbase_granularity_invalid_raises():
    with pytest.raises(ValueError):
        _coinbase_granularity("bogus")


def test_build_feed_coinbase_venue():
    cfg = load_config({"VENUE": "coinbase", "COINBASE_API_KEY": "k", "COINBASE_API_SECRET": "s"})
    feed = build_feed(cfg)
    assert isinstance(feed, CoinbaseCandleFeed)
