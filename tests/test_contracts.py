"""Contract metadata and notional maths (#124).

Risk caps and PnL are ``quantity x price x multiplier``. Before this, an
unknown derivative silently got a multiplier of 1.0 -- a plausible-looking
default that is wrong by the contract size, which for a CME Bitcoin future is
a factor of 5 and for a micro is a factor of 0.1. These tests pin the two
things that prevents: metadata is resolved from the venue, and anything
unresolved refuses rather than guesses.
"""

from __future__ import annotations

import pytest

from tradingbot.venues.contracts import (
    ContractMetadataError,
    ContractSpec,
    spot_spec,
)


class TestLinearNotional:
    """Linear: the contract is denominated in the base asset."""

    def test_notional_scales_with_price_and_size(self) -> None:
        # 2 contracts x 5 BTC per contract x $30,000 = $300,000.
        spec = ContractSpec(
            symbol="BTC/USD", contract_size=5.0, linear=True,
            quote_currency="USD", settle_currency="USD", tick_size=0.01,
            is_derivative=True,
        )

        assert spec.notional(2.0, 30_000.0) == pytest.approx(300_000.0)

    def test_a_micro_contract_is_not_a_full_size_one(self) -> None:
        # The exact error #124 exists to stop: 0.1 BTC per contract, not 1.
        micro = ContractSpec(
            symbol="MBT", contract_size=0.1, linear=True, quote_currency="USD",
            settle_currency="USD", tick_size=5.0, is_derivative=True,
        )

        assert micro.notional(1.0, 30_000.0) == pytest.approx(3_000.0)


class TestInverseNotional:
    """Inverse: the contract is a fixed amount of the quote currency.

    A BitMEX-style inverse BTC/USD contract is worth $1 or $100 regardless of
    price, so its quote notional does not scale with price at all. Applying
    the linear formula would multiply the exposure by the price -- an error of
    four or five orders of magnitude.
    """

    def test_quote_notional_is_price_independent(self) -> None:
        spec = ContractSpec(
            symbol="BTC/USD:BTC", contract_size=100.0, linear=False,
            quote_currency="USD", settle_currency="BTC", tick_size=0.5,
            is_derivative=True,
        )

        assert spec.notional(3.0, 30_000.0) == pytest.approx(300.0)
        assert spec.notional(3.0, 60_000.0) == pytest.approx(300.0)

    def test_inverse_and_linear_disagree_by_the_price(self) -> None:
        kwargs = dict(
            symbol="BTC/USD", contract_size=100.0, quote_currency="USD",
            settle_currency="USD", tick_size=0.5, is_derivative=True,
        )
        linear = ContractSpec(linear=True, **kwargs)  # type: ignore[arg-type]
        inverse = ContractSpec(linear=False, **kwargs)  # type: ignore[arg-type]

        assert linear.notional(1.0, 30_000.0) == pytest.approx(
            inverse.notional(1.0, 30_000.0) * 30_000.0
        )


class TestSpot:
    def test_spot_spec_is_a_plain_multiplier_of_one(self) -> None:
        spec = spot_spec("BTC/USD", quote_currency="USD")

        assert spec.contract_size == 1.0
        assert spec.is_derivative is False
        assert spec.notional(0.5, 30_000.0) == pytest.approx(15_000.0)

    def test_spot_is_always_linear(self) -> None:
        assert spot_spec("BTC/USD", quote_currency="USD").linear is True


class TestValidation:
    """A spec that cannot be trusted must not be constructible."""

    @pytest.mark.parametrize("size", [0.0, -1.0, float("nan"), float("inf")])
    def test_a_nonsensical_contract_size_is_refused(self, size: float) -> None:
        with pytest.raises(ContractMetadataError):
            ContractSpec(
                symbol="X", contract_size=size, linear=True, quote_currency="USD",
                settle_currency="USD", tick_size=None, is_derivative=True,
            )

    def test_a_missing_quote_currency_is_refused(self) -> None:
        with pytest.raises(ContractMetadataError):
            ContractSpec(
                symbol="X", contract_size=1.0, linear=True, quote_currency="",
                settle_currency="USD", tick_size=None, is_derivative=True,
            )

    def test_notional_refuses_a_nonsensical_price(self) -> None:
        spec = spot_spec("BTC/USD", quote_currency="USD")

        for price in (0.0, -1.0, float("nan")):
            with pytest.raises(ContractMetadataError):
                spec.notional(1.0, price)

    def test_notional_refuses_a_nonsensical_quantity(self) -> None:
        spec = spot_spec("BTC/USD", quote_currency="USD")

        with pytest.raises(ContractMetadataError):
            spec.notional(float("nan"), 100.0)


class TestDescription:
    def test_a_spec_describes_itself_for_the_operator(self) -> None:
        # Surfaced in start-up errors, so it has to name the numbers that
        # would otherwise be silently wrong.
        spec = ContractSpec(
            symbol="MBTF6", contract_size=0.1, linear=True, quote_currency="USD",
            settle_currency="USD", tick_size=5.0, is_derivative=True,
        )

        described = spec.describe()
        assert "0.1" in described
        assert "linear" in described.lower()
