"""Venue capabilities and strategy requirements (#125).

Strategies and venues were flat name lists, so any strategy could be paired
with any venue. `SignalRouter` mapped a sell action straight to a sell order
and never read `Signal.position_side`, while spot is long-only in practice --
so a short-capable strategy on spot would either fail at the exchange or,
worse, sell inventory to express a "short" it cannot actually hold.

These tests pin that the pairing is checked before anything is submitted.
"""

from __future__ import annotations

import pytest

from tradingbot.models import Action, OrderType, PositionSide, Signal
from tradingbot.venues.capabilities import (
    CapabilityError,
    StrategyRequirements,
    VenueCapabilities,
    check_signal,
    check_strategy,
)


def _spot() -> VenueCapabilities:
    return VenueCapabilities(
        venue="coinbase", market_type="spot", supports_short=False,
        supports_reduce_only=False,
        order_types=frozenset({OrderType.market, OrderType.limit}),
    )


def _futures() -> VenueCapabilities:
    return VenueCapabilities(
        venue="coinbase", market_type="futures", supports_short=True,
        supports_reduce_only=True,
        order_types=frozenset({OrderType.market, OrderType.limit}),
    )


def _signal(action: Action, side: PositionSide, **kw) -> Signal:
    return Signal(
        strategy="s", action=action, symbol="BTC/USD",
        order_type=kw.pop("order_type", OrderType.market),
        quantity=1.0, position_side=side, **kw,
    )


class TestSpotIsLongOnly:
    """The headline criterion: spot short signals never reach the venue."""

    def test_a_short_signal_is_rejected_on_spot(self) -> None:
        with pytest.raises(CapabilityError, match="short"):
            check_signal(_signal(Action.sell, PositionSide.short), _spot())

    def test_a_short_buy_is_also_rejected_on_spot(self) -> None:
        # Buying to open a short is incoherent anywhere, and doubly so here.
        with pytest.raises(CapabilityError):
            check_signal(_signal(Action.buy, PositionSide.short), _spot())

    def test_a_long_buy_is_allowed_on_spot(self) -> None:
        check_signal(_signal(Action.buy, PositionSide.long), _spot())

    def test_selling_to_flatten_a_long_is_allowed_on_spot(self) -> None:
        # A sell targeting flat is a disposal, not a short.
        check_signal(_signal(Action.sell, PositionSide.flat), _spot())

    def test_closing_is_allowed_on_spot(self) -> None:
        check_signal(_signal(Action.close, PositionSide.flat), _spot())

    def test_a_close_is_never_blocked_even_declaring_short_on_spot(self) -> None:
        """A close only reduces risk, so refusing one is the worse failure.

        Found by mutation testing: exempting close from every check broke no
        test, which meant nothing pinned it -- and the implementation was in
        fact blocking this, which could strand a position a strategy is
        trying to exit.
        """
        check_signal(_signal(Action.close, PositionSide.short), _spot())

    def test_a_close_is_not_blocked_by_an_unsupported_order_type(self) -> None:
        venue = VenueCapabilities(
            venue="v", market_type="spot", supports_short=False,
            supports_reduce_only=False, order_types=frozenset({OrderType.market}),
        )

        check_signal(
            _signal(Action.close, PositionSide.flat,
                    order_type=OrderType.limit, price=100.0),
            venue,
        )

    def test_a_short_signal_is_allowed_on_futures(self) -> None:
        check_signal(_signal(Action.sell, PositionSide.short), _futures())


class TestActionAndSideCoherence:
    """An action must agree with the position it claims to be reaching."""

    def test_buying_toward_a_long_is_coherent(self) -> None:
        check_signal(_signal(Action.buy, PositionSide.long), _futures())

    def test_selling_toward_a_short_is_coherent(self) -> None:
        check_signal(_signal(Action.sell, PositionSide.short), _futures())

    def test_buying_toward_a_short_is_incoherent(self) -> None:
        with pytest.raises(CapabilityError, match="buy|short"):
            check_signal(_signal(Action.buy, PositionSide.short), _futures())

    def test_selling_toward_a_long_is_incoherent(self) -> None:
        with pytest.raises(CapabilityError, match="sell|long"):
            check_signal(_signal(Action.sell, PositionSide.long), _futures())

    def test_a_close_may_target_any_side(self) -> None:
        # Closing is defined by the position held, not by a target side.
        for side in (PositionSide.long, PositionSide.short, PositionSide.flat):
            check_signal(_signal(Action.close, side), _futures())


class TestOrderTypes:
    def test_an_unsupported_order_type_is_rejected(self) -> None:
        venue = VenueCapabilities(
            venue="v", market_type="spot", supports_short=False,
            supports_reduce_only=False, order_types=frozenset({OrderType.market}),
        )

        with pytest.raises(CapabilityError, match="limit"):
            check_signal(
                _signal(Action.buy, PositionSide.long,
                        order_type=OrderType.limit, price=100.0),
                venue,
            )

    def test_a_supported_order_type_passes(self) -> None:
        check_signal(
            _signal(Action.buy, PositionSide.long,
                    order_type=OrderType.limit, price=100.0),
            _spot(),
        )


class TestStrategyRequirements:
    def test_a_strategy_needing_short_is_refused_on_spot(self) -> None:
        needs_short = StrategyRequirements(requires_short=True)

        with pytest.raises(CapabilityError, match="short"):
            check_strategy("meanrev", needs_short, _spot())

    def test_a_strategy_needing_short_is_allowed_on_futures(self) -> None:
        check_strategy("meanrev", StrategyRequirements(requires_short=True), _futures())

    def test_a_strategy_with_no_requirements_runs_anywhere(self) -> None:
        check_strategy("example", StrategyRequirements(), _spot())
        check_strategy("example", StrategyRequirements(), _futures())

    def test_a_strategy_needing_reduce_only_is_refused_on_spot(self) -> None:
        with pytest.raises(CapabilityError, match="reduce-only|reduce_only"):
            check_strategy(
                "s", StrategyRequirements(requires_reduce_only=True), _spot()
            )

    def test_a_strategy_needing_an_absent_order_type_is_refused(self) -> None:
        venue = VenueCapabilities(
            venue="v", market_type="spot", supports_short=False,
            supports_reduce_only=False, order_types=frozenset({OrderType.market}),
        )

        with pytest.raises(CapabilityError, match="limit"):
            check_strategy(
                "s",
                StrategyRequirements(required_order_types=frozenset({OrderType.limit})),
                venue,
            )

    def test_the_refusal_names_the_strategy_and_the_venue(self) -> None:
        with pytest.raises(CapabilityError) as excinfo:
            check_strategy(
                "meanrev", StrategyRequirements(requires_short=True), _spot()
            )

        message = str(excinfo.value)
        assert "meanrev" in message
        assert "coinbase" in message and "spot" in message


class TestDescription:
    def test_capabilities_describe_themselves(self) -> None:
        described = _spot().describe()

        assert "coinbase" in described
        assert "spot" in described
