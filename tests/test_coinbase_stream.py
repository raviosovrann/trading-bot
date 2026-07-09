import pytest

from tradingbot.models import Candle
from tradingbot.stream import CoinbaseStreamFeed


class _FakeWSClient:
    def __init__(self):
        self.opened = 0
        self.subscribed = []
        self.ran = 0
        self.closed = 0

    def open(self):
        self.opened += 1

    def subscribe(self, product_ids=None, channels=None):
        self.subscribed.append((product_ids, channels))

    def run_forever_with_exception_check(self):
        self.ran += 1

    def close(self):
        self.closed += 1


class _FakeWarmupFeed:
    def __init__(self, candles=None):
        self.calls = []
        self._candles = candles or []

    def warmup_candles(self, symbol, timeframe, limit):
        self.calls.append((symbol, timeframe, limit))
        return list(self._candles)


def _cb_message(start, o=100, h=110, l=90, c=105, v=10, product="BTC-USD", channel="candles"):
    return {
        "channel": channel,
        "events": [
            {
                "type": "update",
                "candles": [
                    {
                        "start": str(start),
                        "open": str(o),
                        "high": str(h),
                        "low": str(l),
                        "close": str(c),
                        "volume": str(v),
                        "product_id": product,
                    }
                ],
            }
        ],
    }


def _make(client=None, warmup_feed=None):
    return CoinbaseStreamFeed(
        client=client or _FakeWSClient(),
        warmup_feed=warmup_feed or _FakeWarmupFeed(),
    )


def test_message_invokes_registered_handler():
    feed = _make()
    received = []
    feed.on_bar(received.append)

    feed._on_ws_message(_cb_message(1704067200))

    assert len(received) == 1
    assert isinstance(received[0], Candle)


def test_duplicate_start_not_re_emitted():
    feed = _make()
    received = []
    feed.on_bar(received.append)

    feed._on_ws_message(_cb_message(1704067200))
    feed._on_ws_message(_cb_message(1704067200))

    assert len(received) == 1


def test_non_candles_channel_ignored():
    feed = _make()
    received = []
    feed.on_bar(received.append)

    feed._on_ws_message(_cb_message(1704067200, channel="heartbeats"))

    assert received == []


def test_warmup_fetches_history_via_rest_not_stream():
    client = _FakeWSClient()
    warmup = _FakeWarmupFeed(candles=[Candle(timestamp=1, open=1, high=1, low=1, close=1, volume=1)])
    feed = CoinbaseStreamFeed(client=client, warmup_feed=warmup)

    candles = feed.warmup_candles("BTC-USD", "5Min", 3)

    assert warmup.calls == [("BTC-USD", "5Min", 3)]
    assert candles == warmup._candles
    assert client.subscribed == []


def test_stop_closes_client():
    client = _FakeWSClient()
    feed = _make(client=client)

    feed.stop()

    assert client.closed == 1


def test_handler_receives_normalized_candle():
    feed = _make()
    received = []
    feed.on_bar(received.append)

    feed._on_ws_message(_cb_message(1704067200, o=100, h=120, l=95, c=110, v=42))

    candle = received[0]
    assert candle.timestamp == 1704067200 * 1000
    assert candle.open == 100.0
    assert candle.high == 120.0
    assert candle.low == 95.0
    assert candle.close == 110.0
    assert candle.volume == 42.0


def test_requires_client():
    with pytest.raises(ValueError):
        CoinbaseStreamFeed(client=None, warmup_feed=_FakeWarmupFeed())
