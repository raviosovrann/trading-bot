from __future__ import annotations

from tradingbot.models import Order, OrderResult, OrderType, Position, PositionSide, Side
from tradingbot.service.risk import GlobalExposure, RiskGuard


class _FakeVenue:
    def __init__(self, *, filled_qty: float | None = None) -> None:
        self.orders: list[Order] = []
        self.close_calls: list[str] = []
        self.position_calls: list[str] = []
        self.health_calls = 0
        self.filled_qty = filled_qty

    def place_order(self, order: Order) -> OrderResult:
        self.orders.append(order)
        return OrderResult(
            ok=True,
            order_id="order-1",
            status="filled",
            filled_qty=order.qty if self.filled_qty is None else self.filled_qty,
            raw={},
        )

    def close_position(self, symbol: str) -> OrderResult:
        self.close_calls.append(symbol)
        return OrderResult(
            ok=True,
            order_id="close-1",
            status="filled",
            filled_qty=1.0,
            raw={},
        )

    def get_position(self, symbol: str) -> Position | None:
        self.position_calls.append(symbol)
        return Position(symbol=symbol, side=PositionSide.long, size=1.0, entry_price=100.0)

    def health_check(self) -> bool:
        self.health_calls += 1
        return True


def _order(*, qty: float = 1.0, reduce_only: bool = False) -> Order:
    return Order(
        symbol="BTC/USD",
        side=Side.buy,
        order_type=OrderType.market,
        qty=qty,
        reduce_only=reduce_only,
    )


def _result_notional(result: OrderResult) -> float:
    return float(result.raw["notional"])


def test_within_cap_delegates_and_increases_global_exposure() -> None:
    venue = _FakeVenue()
    exposure = GlobalExposure()
    guard = RiskGuard(
        venue,
        per_bot_cap=150.0,
        global_cap=200.0,
        global_state=exposure,
        price_source=lambda: 100.0,
        multiplier=2.0,
    )

    result = guard.place_order(_order(qty=0.5))

    assert result.ok is True
    assert venue.orders == [_order(qty=0.5)]
    assert exposure.used == 100.0


def test_per_bot_cap_violation_is_blocked_without_calling_venue() -> None:
    venue = _FakeVenue()
    result = RiskGuard(
        venue,
        per_bot_cap=99.0,
        global_cap=1_000.0,
        global_state=GlobalExposure(),
        price_source=lambda: 100.0,
    ).place_order(_order())

    assert result == OrderResult(
        ok=False,
        order_id=None,
        status="risk_blocked",
        filled_qty=0.0,
        raw={"notional": 100.0},
        error="notional cap exceeded",
    )
    assert venue.orders == []


def test_global_cap_violation_is_blocked() -> None:
    venue = _FakeVenue()
    exposure = GlobalExposure(used=75.0)
    result = RiskGuard(
        venue,
        per_bot_cap=100.0,
        global_cap=100.0,
        global_state=exposure,
        price_source=lambda: 30.0,
    ).place_order(_order(qty=1.0))

    assert result.status == "risk_blocked"
    assert _result_notional(result) == 30.0
    assert exposure.used == 75.0
    assert venue.orders == []


def test_reduce_only_always_delegates_and_decreases_exposure() -> None:
    venue = _FakeVenue()
    exposure = GlobalExposure(used=100.0)
    guard = RiskGuard(
        venue,
        per_bot_cap=1.0,
        global_cap=1.0,
        global_state=exposure,
        price_source=lambda: 100.0,
        multiplier=2.0,
    )

    result = guard.place_order(_order(qty=0.25, reduce_only=True))

    assert result.ok is True
    assert len(venue.orders) == 1
    assert exposure.used == 50.0


def test_reduction_uses_confirmed_filled_quantity() -> None:
    venue = _FakeVenue(filled_qty=0.1)
    exposure = GlobalExposure(used=100.0)
    guard = RiskGuard(
        venue,
        per_bot_cap=1.0,
        global_cap=1.0,
        global_state=exposure,
        price_source=lambda: 100.0,
        multiplier=2.0,
    )

    result = guard.place_order(_order(qty=0.25, reduce_only=True))

    assert result.ok is True
    assert exposure.used == 80.0


def test_unfilled_reduction_does_not_decrease_exposure() -> None:
    venue = _FakeVenue(filled_qty=0.0)
    exposure = GlobalExposure(used=100.0)
    guard = RiskGuard(
        venue,
        per_bot_cap=1.0,
        global_cap=1.0,
        global_state=exposure,
        price_source=lambda: 100.0,
    )

    result = guard.place_order(_order(reduce_only=True))

    assert result.ok is True
    assert exposure.used == 100.0


def test_reduce_only_bypasses_missing_price() -> None:
    venue = _FakeVenue()
    guard = RiskGuard(
        venue,
        per_bot_cap=0.0,
        global_cap=0.0,
        global_state=GlobalExposure(),
        price_source=lambda: None,
    )

    result = guard.place_order(_order(reduce_only=True))

    assert result.ok is True
    assert len(venue.orders) == 1


def test_missing_price_fails_safe() -> None:
    venue = _FakeVenue()
    result = RiskGuard(
        venue,
        per_bot_cap=1_000.0,
        global_cap=1_000.0,
        global_state=GlobalExposure(),
        price_source=lambda: None,
    ).place_order(_order())

    assert result.ok is False
    assert result.status == "risk_blocked"
    assert result.error == "price or order size unavailable"
    assert result.raw == {"notional": 0.0}
    assert venue.orders == []


def test_invalid_order_size_fails_safe() -> None:
    venue = _FakeVenue()
    result = RiskGuard(
        venue,
        per_bot_cap=1_000.0,
        global_cap=1_000.0,
        global_state=GlobalExposure(),
        price_source=lambda: 100.0,
        multiplier=float("nan"),
    ).place_order(_order(qty=float("nan")))

    assert result.status == "risk_blocked"
    assert result.error == "price or order size unavailable"
    assert venue.orders == []


def test_execution_venue_methods_delegate() -> None:
    venue = _FakeVenue()
    guard = RiskGuard(
        venue,
        per_bot_cap=100.0,
        global_cap=100.0,
        global_state=GlobalExposure(),
        price_source=lambda: 100.0,
    )

    close_result = guard.close_position("BTC/USD")
    position = guard.get_position("BTC/USD")
    healthy = guard.health_check()

    assert close_result.order_id == "close-1"
    assert position is not None and position.symbol == "BTC/USD"
    assert healthy is True
    assert venue.close_calls == ["BTC/USD"]
    assert venue.position_calls == ["BTC/USD"]
    assert venue.health_calls == 1