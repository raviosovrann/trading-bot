import pytest

from tradingbot.datafeed import InMemoryCandleFeed
from tradingbot.models import Action, Candle, OrderResult, OrderType, PositionSide, Signal
from tradingbot.router import SignalRouter
from tradingbot.runtime import BotRuntime


class StubStrategy:
    def __init__(self, signal: Signal | None) -> None:
        self.signal = signal
        self.calls = 0

    def on_bar(self, candles):
        self.calls += 1
        del candles
        return self.signal


class StubVenue:
    def __init__(self):
        self.orders = []

    def place_order(self, order):
        self.orders.append(order)
        return OrderResult(ok=True, order_id="ord-1", status="filled", filled_qty=order.qty, raw={})

    def close_position(self, symbol: str):
        return OrderResult(ok=True, order_id="ord-2", status=f"closed:{symbol}", filled_qty=0.0, raw={})

    def get_position(self, symbol: str):
        del symbol
        return None

    def health_check(self):
        return True


class BrokenFeed:
    def warmup_candles(self, symbol: str, timeframe: str, limit: int):
        del symbol, timeframe, limit
        return []

    def latest_closed_candle(self, symbol: str, timeframe: str):
        del symbol, timeframe
        raise RuntimeError("feed boom")


def _candle(ts: int, close: float) -> Candle:
    return Candle(timestamp=ts, open=close, high=close, low=close, close=close, volume=1.0)


def test_runtime_run_once_processes_candle_signal_and_order():
    symbol = "BTC/USD"
    feed = InMemoryCandleFeed({symbol: [_candle(1, 100.0)]})
    signal = Signal(
        strategy="sma",
        action=Action.buy,
        symbol=symbol,
        order_type=OrderType.market,
        quantity=0.01,
        position_side=PositionSide.long,
    )
    strategy = StubStrategy(signal)
    venue = StubVenue()
    router = SignalRouter(venue)
    runtime = BotRuntime(feed=feed, strategy=strategy, router=router, symbol=symbol, timeframe="5Min")

    result = runtime.run_once()

    assert result is not None and result.ok is True
    assert strategy.calls == 1
    assert len(venue.orders) == 1


def test_runtime_run_once_no_signal_returns_none_and_no_order():
    symbol = "BTC/USD"
    feed = InMemoryCandleFeed({symbol: [_candle(1, 100.0)]})
    strategy = StubStrategy(None)
    venue = StubVenue()
    runtime = BotRuntime(feed=feed, strategy=strategy, router=SignalRouter(venue), symbol=symbol, timeframe="5Min")

    result = runtime.run_once()

    assert result is None
    assert strategy.calls == 1
    assert venue.orders == []


def test_runtime_run_forever_swallow_exceptions_with_injected_sleep():
    sleep_calls = []
    seen_errors = []

    runtime = BotRuntime(
        feed=BrokenFeed(),
        strategy=StubStrategy(None),
        router=SignalRouter(StubVenue()),
        symbol="BTC/USD",
        timeframe="5Min",
    )

    results = runtime.run_forever(
        sleep_seconds=0.0,
        sleep_fn=lambda seconds: sleep_calls.append(seconds),
        max_iterations=3,
        swallow_exceptions=True,
        on_exception=lambda exc: seen_errors.append(str(exc)),
    )

    assert results == []
    assert sleep_calls == [0.0, 0.0, 0.0]
    assert len(seen_errors) == 3
    assert all("feed boom" in msg for msg in seen_errors)


def test_runtime_run_forever_raises_when_not_swallowing_exceptions():
    runtime = BotRuntime(
        feed=BrokenFeed(),
        strategy=StubStrategy(None),
        router=SignalRouter(StubVenue()),
        symbol="BTC/USD",
        timeframe="5Min",
    )

    with pytest.raises(RuntimeError, match="feed boom"):
        runtime.run_forever(
            sleep_seconds=0.0,
            sleep_fn=lambda _seconds: None,
            max_iterations=1,
            swallow_exceptions=False,
        )
