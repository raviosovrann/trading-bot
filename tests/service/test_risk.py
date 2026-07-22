"""Tests for the risk guard and global exposure."""

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


def _contract(size: float):
    """A linear derivative spec with the given contract size.

    Replaces the old bare ``multiplier=`` argument (#124). A NaN or zero size
    is no longer expressible here at all -- ContractSpec validates on
    construction -- which is why the old NaN-multiplier case is gone; see
    tests/test_contracts.py for the refusal.
    """
    from tradingbot.venues.contracts import ContractSpec
    return ContractSpec(
        symbol="BTC/USD", contract_size=size, linear=True, quote_currency="USD",
        settle_currency="USD", tick_size=None, is_derivative=True,
    )


def _result_notional(result: OrderResult) -> float:
    return float(result.raw["notional"])


def test_within_cap_delegates_and_increases_global_exposure() -> None:
    """Verify that an order within the cap delegates and increases global exposure."""
    venue = _FakeVenue()
    exposure = GlobalExposure()
    guard = RiskGuard(
        venue,
        per_bot_cap=150.0,
        global_cap=200.0,
        global_state=exposure,
        price_source=lambda: 100.0,
        contract=_contract(2.0),
    )

    result = guard.place_order(_order(qty=0.5))

    assert result.ok is True
    assert venue.orders == [_order(qty=0.5)]
    assert exposure.used == 100.0


def test_per_bot_cap_violation_is_blocked_without_calling_venue() -> None:
    """Verify that a per-bot cap violation is blocked without calling the venue."""
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
    """Verify that a global cap violation is blocked."""
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
    """Verify that reduce-only orders always delegate and decrease exposure."""
    venue = _FakeVenue()
    exposure = GlobalExposure(used=100.0)
    guard = RiskGuard(
        venue,
        per_bot_cap=1.0,
        global_cap=1.0,
        global_state=exposure,
        price_source=lambda: 100.0,
        contract=_contract(2.0),
    )

    result = guard.place_order(_order(qty=0.25, reduce_only=True))

    assert result.ok is True
    assert len(venue.orders) == 1
    assert exposure.used == 50.0


def test_reduction_uses_confirmed_filled_quantity() -> None:
    """Verify that exposure reduction uses the confirmed filled quantity."""
    venue = _FakeVenue(filled_qty=0.1)
    exposure = GlobalExposure(used=100.0)
    guard = RiskGuard(
        venue,
        per_bot_cap=1.0,
        global_cap=1.0,
        global_state=exposure,
        price_source=lambda: 100.0,
        contract=_contract(2.0),
    )

    result = guard.place_order(_order(qty=0.25, reduce_only=True))

    assert result.ok is True
    assert exposure.used == 80.0


def test_unfilled_reduction_does_not_decrease_exposure() -> None:
    """Verify that an unfilled reduction does not decrease exposure."""
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
    """Verify that reduce-only orders bypass the missing price check."""
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
    """Verify that a missing price fails safely with a risk-blocked result."""
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
    """Verify that an invalid order size fails safely with a risk-blocked result."""
    venue = _FakeVenue()
    result = RiskGuard(
        venue,
        per_bot_cap=1_000.0,
        global_cap=1_000.0,
        global_state=GlobalExposure(),
        price_source=lambda: 100.0,
    ).place_order(_order(qty=float("nan")))

    assert result.status == "risk_blocked"
    assert result.error == "price or order size unavailable"
    assert venue.orders == []


def test_execution_venue_methods_delegate() -> None:
    """Verify that close_position, get_position and health_check delegate to the venue."""
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

class TestInverseContractExposure:
    """Exposure must use the contract's own convention (#124).

    A bare multiplier can only express the linear formula. An inverse contract
    is a fixed amount of quote currency, so applying `qty x price x size` to
    one inflates its measured exposure by the price -- tens of thousands of
    times over for a crypto pair, which would let a cap be blown or block a
    legitimate order depending on direction.
    """

    def _spec(self, *, linear: bool, size: float):
        from tradingbot.venues.contracts import ContractSpec
        return ContractSpec(
            symbol="BTC/USD", contract_size=size, linear=linear,
            quote_currency="USD", settle_currency="USD", tick_size=None,
            is_derivative=True,
        )

    def _guard(self, spec, *, cap: float, price: float = 30_000.0):
        venue = _FakeVenue()
        return venue, RiskGuard(
            venue, per_bot_cap=cap, global_cap=cap * 10,
            global_state=GlobalExposure(), price_source=lambda: price,
            contract=spec,
        )

    def test_linear_exposure_scales_with_price(self):
        spec = self._spec(linear=True, size=0.1)
        # 1 contract x 0.1 BTC x $30,000 = $3,000, inside a $5,000 cap.
        venue, guard = self._guard(spec, cap=5_000.0)

        result = guard.place_order(_order(qty=1.0))

        assert result.ok is True
        assert len(venue.orders) == 1

    def test_linear_exposure_over_the_cap_is_blocked(self):
        spec = self._spec(linear=True, size=0.1)
        venue, guard = self._guard(spec, cap=1_000.0)

        result = guard.place_order(_order(qty=1.0))

        assert result.ok is False
        assert result.status == "risk_blocked"
        assert venue.orders == []

    def test_inverse_exposure_does_not_scale_with_price(self):
        # 3 contracts x $100 each = $300, regardless of a $30,000 price.
        # The linear formula would make this $9,000,000 and block it.
        spec = self._spec(linear=False, size=100.0)
        venue, guard = self._guard(spec, cap=1_000.0)

        result = guard.place_order(_order(qty=3.0))

        assert result.ok is True, "inverse notional must not be multiplied by price"
        assert len(venue.orders) == 1

    def test_inverse_exposure_still_respects_the_cap(self):
        spec = self._spec(linear=False, size=100.0)
        venue, guard = self._guard(spec, cap=200.0)

        result = guard.place_order(_order(qty=3.0))

        assert result.ok is False
        assert venue.orders == []

    def test_a_spot_guard_still_works_without_a_spec(self):
        """Callers that pass no contract keep plain qty x price."""
        venue = _FakeVenue()
        guard = RiskGuard(
            venue, per_bot_cap=5_000.0, global_cap=50_000.0,
            global_state=GlobalExposure(), price_source=lambda: 100.0,
        )

        assert guard.place_order(_order(qty=1.0)).ok is True
