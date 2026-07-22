"""Tests for the bot runtime run loop."""

from doubles import InMemoryCandleFeed
from tradingbot.models import Action, Candle, OrderResult, OrderType, PositionSide, Signal
from tradingbot.router import SignalRouter
from tradingbot.runtime import CandleProcessor


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


def _candle(ts: int, close: float) -> Candle:
    return Candle(timestamp=ts, open=close, high=close, low=close, close=close, volume=1.0)


def test_evaluate_processes_candle_signal_and_order():
    """Verify evaluate() runs the strategy on the buffer and routes its signal."""
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
    proc = CandleProcessor(strategy=strategy, router=router, event_symbol=symbol)
    proc.seed(feed.warmup_candles(symbol, "5Min", 10))

    result = proc.evaluate()

    assert result is not None and result.ok is True
    assert strategy.calls == 1
    assert len(venue.orders) == 1


def test_no_signal_returns_none_and_no_order():
    """Verify evaluate() returns None and places no order when the strategy is silent."""
    symbol = "BTC/USD"
    feed = InMemoryCandleFeed({symbol: [_candle(1, 100.0)]})
    strategy = StubStrategy(None)
    venue = StubVenue()
    proc = CandleProcessor(
        strategy=strategy, router=SignalRouter(venue), event_symbol=symbol
    )
    proc.seed(feed.warmup_candles(symbol, "5Min", 10))

    result = proc.evaluate()

    assert result is None
    assert strategy.calls == 1
    assert venue.orders == []


def test_process_candle_appends_and_routes_signal():
    """Verify that process_candle appends to the buffer and routes the generated signal."""
    symbol = "BTC/USD"
    feed = InMemoryCandleFeed({symbol: []})
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
    proc = CandleProcessor(
        strategy=strategy, router=SignalRouter(venue), event_symbol=symbol
    )

    result = proc.process_candle(_candle(1, 100.0))

    assert result is not None and result.ok is True
    assert strategy.calls == 1
    assert len(venue.orders) == 1
    assert len(proc.candles) == 1


def test_process_candle_dedups_stale_timestamp_and_does_not_route():
    """Verify that process_candle ignores duplicate timestamps and does not route twice."""
    symbol = "BTC/USD"
    feed = InMemoryCandleFeed({symbol: []})
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
    proc = CandleProcessor(
        strategy=strategy, router=SignalRouter(venue), event_symbol=symbol
    )

    proc.process_candle(_candle(5, 100.0))
    result = proc.process_candle(_candle(5, 101.0))

    assert result is None
    assert strategy.calls == 1
    assert len(venue.orders) == 1
    assert len(proc.candles) == 1


def test_process_candle_no_signal_returns_none_and_no_order():
    """Verify that process_candle returns None and places no order when the strategy is silent."""
    symbol = "BTC/USD"
    feed = InMemoryCandleFeed({symbol: []})
    strategy = StubStrategy(None)
    venue = StubVenue()
    proc = CandleProcessor(
        strategy=strategy, router=SignalRouter(venue), event_symbol=symbol
    )

    result = proc.process_candle(_candle(1, 100.0))

    assert result is None
    assert strategy.calls == 1
    assert venue.orders == []
    assert len(proc.candles) == 1
