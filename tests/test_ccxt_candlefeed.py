from tradingbot.datafeed import CcxtCandleFeed
from tradingbot.models import Candle


class _FakeExchange:
    """Minimal ccxt-like stub: records fetch_ohlcv calls, returns canned rows."""

    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    def fetch_ohlcv(self, symbol, timeframe, limit=None):
        self.calls.append((symbol, timeframe, limit))
        return list(self._rows)


def _row(ts, o, h, l, c, v):
    # ccxt OHLCV row shape: [timestamp_ms, open, high, low, close, volume]
    return [ts, o, h, l, c, v]


def test_construct_requires_exchange():
    import pytest

    with pytest.raises(ValueError):
        CcxtCandleFeed(None)


def test_warmup_maps_ohlcv_rows_to_candles():
    # 4 rows returned; with limit=3 we ask for 4 (limit+1) and drop the forming last bar.
    rows = [
        _row(1000, 1.0, 2.0, 0.5, 1.5, 10.0),
        _row(2000, 1.5, 2.5, 1.0, 2.0, 11.0),
        _row(3000, 2.0, 3.0, 1.5, 2.5, 12.0),
        _row(4000, 2.5, 3.5, 2.0, 3.0, 13.0),  # still-forming, must be dropped
    ]
    ex = _FakeExchange(rows)
    feed = CcxtCandleFeed(ex)

    candles = feed.warmup_candles("BTC/USD", "5m", 3)

    assert ex.calls == [("BTC/USD", "5m", 4)]  # fetched limit+1
    assert len(candles) == 3
    assert all(isinstance(c, Candle) for c in candles)
    # forming bar (ts=4000) dropped; newest closed is ts=3000
    assert [c.timestamp for c in candles] == [1000, 2000, 3000]
    first = candles[0]
    assert (first.open, first.high, first.low, first.close, first.volume) == (1.0, 2.0, 0.5, 1.5, 10.0)


def test_warmup_zero_limit_returns_empty():
    ex = _FakeExchange([_row(1000, 1, 2, 0, 1, 5)])
    feed = CcxtCandleFeed(ex)
    assert feed.warmup_candles("BTC/USD", "5m", 0) == []
    assert ex.calls == []  # no fetch when nothing requested


def test_latest_closed_skips_forming_bar():
    rows = [
        _row(1000, 1.0, 2.0, 0.5, 1.5, 10.0),  # last closed
        _row(2000, 1.5, 2.5, 1.0, 2.0, 11.0),  # forming
    ]
    ex = _FakeExchange(rows)
    feed = CcxtCandleFeed(ex)

    candle = feed.latest_closed_candle("ETH/USD", "1h")

    assert ex.calls == [("ETH/USD", "1h", 2)]
    assert candle is not None
    assert candle.timestamp == 1000  # -2 row, not the forming -1


def test_latest_closed_empty_returns_none():
    ex = _FakeExchange([])
    feed = CcxtCandleFeed(ex)
    assert feed.latest_closed_candle("BTC/USD", "5m") is None


def test_latest_closed_single_row_treated_as_closed():
    ex = _FakeExchange([_row(1000, 1.0, 2.0, 0.5, 1.5, 10.0)])
    feed = CcxtCandleFeed(ex)
    candle = feed.latest_closed_candle("BTC/USD", "5m")
    assert candle is not None and candle.timestamp == 1000
