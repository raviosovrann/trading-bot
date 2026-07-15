"""Tests for signal routing and risk integration."""

from tradingbot.models import Action, OrderResult, OrderType, PositionSide, Side, Signal
from tradingbot.router import SignalRouter
from tradingbot.service.risk import GlobalExposure


class StubVenue:
    def __init__(self) -> None:
        self.placed_orders = []
        self.closed_symbols = []

    def place_order(self, order):
        self.placed_orders.append(order)
        return OrderResult(ok=True, order_id="1", status="filled", filled_qty=order.qty, raw={})

    def close_position(self, symbol: str):
        self.closed_symbols.append(symbol)
        return OrderResult(ok=True, order_id="2", status="closed", filled_qty=0.0, raw={})

    def get_position(self, symbol: str):
        del symbol
        return None

    def health_check(self) -> bool:
        return True


def test_router_maps_buy_signal_to_place_order():
    """Verify that a buy signal is routed to a place_order call."""
    venue = StubVenue()
    router = SignalRouter(venue)
    signal = Signal(
        strategy="sma",
        action=Action.buy,
        symbol="BTC/USD",
        order_type=OrderType.limit,
        price=50000.0,
        quantity=0.01,
        position_side=PositionSide.long,
    )

    result = router.route(signal)

    assert result.ok is True
    assert venue.closed_symbols == []
    assert len(venue.placed_orders) == 1
    order = venue.placed_orders[0]
    assert order.side is Side.buy
    assert order.order_type is OrderType.limit
    assert order.price == 50000.0
    assert order.qty == 0.01


def test_router_maps_close_signal_to_close_position():
    """Verify that a close signal is routed to a close_position call."""
    venue = StubVenue()
    router = SignalRouter(venue)
    signal = Signal(
        strategy="sma",
        action=Action.close,
        symbol="BTC/USD",
        order_type=OrderType.market,
        quantity=0.01,
        position_side=PositionSide.flat,
    )

    result = router.route(signal)

    assert result.ok is True
    assert venue.closed_symbols == ["BTC/USD"]
    assert venue.placed_orders == []


def test_router_with_risk_guard_blocks_orders_over_cap() -> None:
    """Verify that the risk guard blocks orders exceeding the configured cap."""
    venue = StubVenue()
    router = SignalRouter.with_risk_guard(
        venue,
        per_bot_cap=99.0,
        global_cap=100.0,
        global_state=GlobalExposure(),
        price_source=lambda: 100.0,
    )
    signal = Signal(
        strategy="sma",
        action=Action.buy,
        symbol="BTC/USD",
        order_type=OrderType.market,
        quantity=1.0,
        position_side=PositionSide.long,
    )

    result = router.route(signal)

    assert result.status == "risk_blocked"
    assert venue.placed_orders == []
