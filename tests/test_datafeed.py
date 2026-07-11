from doubles import InMemoryCandleFeed, normalize_candle
from tradingbot.models import Candle


def test_normalize_candle_accepts_short_and_long_keys():
    c1 = normalize_candle({"timestamp": 1, "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100})
    c2 = normalize_candle({"t": 2, "o": 20, "h": 22, "l": 19, "c": 21, "v": 200})

    assert isinstance(c1, Candle)
    assert c1.timestamp == 1 and c1.close == 10.5
    assert c2.timestamp == 2 and c2.open == 20.0 and c2.volume == 200.0


def test_inmemory_feed_warmup_then_streams_latest_closed_candles():
    symbol = "BTC/USD"
    feed = InMemoryCandleFeed(
        {
            symbol: [
                {"t": 1, "o": 10, "h": 11, "l": 9, "c": 10, "v": 1},
                {"t": 2, "o": 10, "h": 12, "l": 9, "c": 11, "v": 1},
                {"t": 3, "o": 11, "h": 13, "l": 10, "c": 12, "v": 1},
            ]
        }
    )

    warmup = feed.warmup_candles(symbol, "5Min", 2)
    assert [c.timestamp for c in warmup] == [1, 2]

    latest = feed.latest_closed_candle(symbol, "5Min")
    assert latest is not None
    assert latest.timestamp == 3
    assert feed.latest_closed_candle(symbol, "5Min") is None


def test_inmemory_feed_unknown_symbol_is_empty():
    feed = InMemoryCandleFeed()
    assert feed.warmup_candles("ETH/USD", "5Min", 10) == []
    assert feed.latest_closed_candle("ETH/USD", "5Min") is None
