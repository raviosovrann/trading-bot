import pytest

from tradingbot.config import load_config
from tradingbot.models import Action, Candle, OrderType, PositionSide, Signal
from tradingbot.router import SignalRouter
from tradingbot.runtime import StreamRuntime
from tradingbot.stream import AlpacaStreamFeed, build_stream_feed
from tradingbot.venues.fake import FakeVenue


class _FakeStreamingFeed:
    """StreamingFeed test double; a test can push synthetic bars on demand."""

    def __init__(self, warmup=None):
        self._warmup = warmup or []
        self._handler = None
        self.warmup_calls = []
        self.run_called_with = None
        self.stopped = 0

    def warmup_candles(self, symbol, timeframe, limit):
        self.warmup_calls.append((symbol, timeframe, limit))
        return list(self._warmup)

    def on_bar(self, handler):
        self._handler = handler

    def run(self, *symbols):
        self.run_called_with = symbols

    def stop(self):
        self.stopped += 1

    def push(self, candle):
        assert self._handler is not None, "handler not registered"
        return self._handler(candle)


class _StubStrategy:
    """Emits a market buy when the latest candle's close equals trigger_close."""

    def __init__(self, symbol, trigger_close):
        self.symbol = symbol
        self._trigger = trigger_close

    def on_bar(self, candles):
        if candles[-1].close == self._trigger:
            return Signal(
                strategy="stub",
                action=Action.buy,
                symbol=self.symbol,
                order_type=OrderType.market,
                quantity=0.01,
                position_side=PositionSide.long,
            )
        return None


def _candle(ts, close=100.0):
    return Candle(timestamp=ts, open=close, high=close, low=close, close=close, volume=1.0)


def _make(symbol="BTC/USD", warmup=None, strategy=None):
    feed = _FakeStreamingFeed(warmup=warmup)
    venue = FakeVenue()
    router = SignalRouter(venue)
    strategy = strategy or _StubStrategy(symbol, trigger_close=200.0)
    rt = StreamRuntime(
        feed=feed,
        strategy=strategy,
        router=router,
        symbol=symbol,
        timeframe="5Min",
        warmup_bars=len(warmup or []),
    )
    return rt, feed, venue


def test_stream_runtime_warms_up_once():
    warmup = [_candle(1), _candle(2)]
    rt, feed, venue = _make(warmup=warmup)
    assert feed.warmup_calls == [("BTC/USD", "5Min", 2)]


def test_stream_runtime_registers_handler_before_run():
    rt, feed, venue = _make()
    assert feed._handler is not None


def test_stream_runtime_pushed_bar_drives_strategy_and_router():
    rt, feed, venue = _make()
    feed.push(_candle(10, close=100.0))  # no signal
    assert venue.orders == []
    feed.push(_candle(20, close=200.0))  # trigger buy
    assert len(venue.orders) == 1
    assert venue.get_position("BTC/USD").side is PositionSide.long


def test_stream_runtime_dedups_stale_timestamp():
    rt, feed, venue = _make()
    feed.push(_candle(20, close=200.0))
    feed.push(_candle(20, close=200.0))  # same ts -> ignored
    assert len(venue.orders) == 1


def test_stream_runtime_start_runs_feed_with_symbol():
    rt, feed, venue = _make()
    rt.start(install_signals=False)
    assert feed.run_called_with == ("BTC/USD",)


def test_stream_runtime_stop_calls_feed_stop():
    rt, feed, venue = _make()
    rt.stop()
    assert feed.stopped == 1


def test_stream_runtime_stop_is_idempotent():
    rt, feed, venue = _make()
    rt.stop()
    rt.stop()
    assert feed.stopped == 1


def test_build_stream_feed_alpaca():
    cfg = load_config({"VENUE": "alpaca", "ALPACA_API_KEY": "k", "ALPACA_API_SECRET": "s"})
    feed = build_stream_feed(cfg)
    assert isinstance(feed, AlpacaStreamFeed)


def test_build_stream_feed_coinbase_not_implemented():
    cfg = load_config({"VENUE": "coinbase", "COINBASE_API_KEY": "k", "COINBASE_API_SECRET": "s"})
    with pytest.raises(NotImplementedError):
        build_stream_feed(cfg)


def test_config_stream_flag_defaults_false_and_parses_true():
    assert load_config({}).stream is False
    assert load_config({"STREAM": "true"}).stream is True
