"""Tests for signal routing and risk integration."""

from tradingbot.models import Action, OrderResult, OrderType, PositionSide, Side, Signal
from tradingbot.router import SignalRouter
from tradingbot.service.exposure import ExposureTracker


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
        exposure=ExposureTracker(),
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


def _buy_signal(quantity: float = 0.01) -> Signal:
    return Signal(
        strategy="sma",
        action=Action.buy,
        symbol="BTC/USD",
        order_type=OrderType.market,
        quantity=quantity,
        position_side=PositionSide.long,
    )


def test_router_stamps_every_order_with_a_client_order_id():
    """Orders need an idempotency key before they reach the venue (#135).

    The ledger keys on it, and it is the only identifier that exists before
    the venue answers -- which matters most when the venue never does.
    """
    venue = StubVenue()
    router = SignalRouter(venue)

    router.route(_buy_signal())

    order = venue.placed_orders[0]
    assert order.client_order_id
    assert isinstance(order.client_order_id, str)


def test_client_order_ids_are_unique_per_order():
    venue = StubVenue()
    router = SignalRouter(venue)

    router.route(_buy_signal())
    router.route(_buy_signal())

    first, second = venue.placed_orders
    assert first.client_order_id != second.client_order_id


def test_route_detailed_returns_the_submitted_order_alongside_the_result():
    """The caller has to persist what was sent, not just what came back."""
    venue = StubVenue()
    router = SignalRouter(venue)

    outcome = router.route_detailed(_buy_signal())

    assert outcome.result.ok is True
    assert outcome.order is not None
    assert outcome.order is venue.placed_orders[0]
    assert outcome.order.client_order_id


def test_route_detailed_reports_no_order_for_a_close():
    """A close goes through close_position(), so no Order object exists."""
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

    outcome = router.route_detailed(signal)

    assert outcome.order is None
    assert outcome.result.ok is True
    assert venue.closed_symbols == ["BTC/USD"]


def test_route_still_returns_the_bare_result():
    """route() keeps its signature; route_detailed() is the richer seam."""
    venue = StubVenue()
    router = SignalRouter(venue)

    result = router.route(_buy_signal())

    assert isinstance(result, OrderResult)


class _OwnedQtyVenue(StubVenue):
    """Records how close_position was called."""

    def __init__(self) -> None:
        super().__init__()
        self.close_kwargs: list[dict] = []

    def close_position(self, symbol: str, **kwargs):
        self.close_kwargs.append(kwargs)
        return super().close_position(symbol)


def test_close_passes_the_owned_quantity_when_one_is_known():
    """#128: the venue must be told what this bot owns, not guess."""
    venue = _OwnedQtyVenue()
    router = SignalRouter(venue, owned_qty_source=lambda: 2.0)
    signal = Signal(
        strategy="s", action=Action.close, symbol="BTC/USD",
        order_type=OrderType.market, quantity=1.0, position_side=PositionSide.flat,
    )

    router.route(signal)

    assert venue.close_kwargs == [{"owned_qty": 2.0}]


def test_close_omits_the_owned_quantity_when_none_is_configured():
    """Derivative venues report a real position; leave them to it."""
    venue = _OwnedQtyVenue()
    router = SignalRouter(venue)
    signal = Signal(
        strategy="s", action=Action.close, symbol="BTC/USD",
        order_type=OrderType.market, quantity=1.0, position_side=PositionSide.flat,
    )

    router.route(signal)

    assert venue.close_kwargs == [{}]
