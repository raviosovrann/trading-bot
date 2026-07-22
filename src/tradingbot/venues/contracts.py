"""Contract metadata: what one unit of a tradable instrument actually is.

Risk caps and PnL are ``quantity x price x multiplier``, and until #124 that
multiplier was a guess. Tradovate carried a four-entry hard-coded table and
returned ``1.0`` for anything it did not recognise; ccxt derivatives exposed
no multiplier at all, so the supervisor defaulted them to ``1.0`` too.

``1.0`` is the worst possible default because it is *plausible*. A CME Bitcoin
future is 5 BTC per contract and a Micro Bitcoin is 0.1, so the same wrong
default understates one position by 5x and overstates the other by 10x -- and
nothing about the resulting number looks unusual enough to notice.

So this module does two things: it describes an instrument precisely enough to
compute exposure from, and it refuses to exist in an untrustworthy state. A
spec that cannot be validated raises rather than falling back, which is what
lets the supervisor fail a bot closed instead of trading it on bad arithmetic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


class ContractMetadataError(Exception):
    """Raised when instrument metadata is missing, ambiguous or unusable.

    Deliberately an error rather than a sentinel value. The whole point of
    #124 is that there is no safe default to fall back to.
    """


def _require_positive(value: float, *, field: str, symbol: str) -> float:
    """Return ``value`` if it is a positive finite number, else raise.

    Args:
        value: Candidate number, typically straight off a venue response.
        field: Field name, for the error message.
        symbol: Instrument the value belongs to, for the error message.

    Returns:
        The validated value.

    Raises:
        ContractMetadataError: If the value is not finite and positive.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ContractMetadataError(
            f"{symbol}: {field} is not a number ({value!r})"
        ) from None
    if not math.isfinite(number) or number <= 0:
        raise ContractMetadataError(f"{symbol}: {field} must be > 0, got {number!r}")
    return number


@dataclass(frozen=True)
class ContractSpec:
    """What one contract of an instrument represents.

    Validated on construction, so holding one is evidence that the venue
    actually told us these numbers rather than that we assumed them.
    """

    symbol: str
    """Instrument symbol as the venue names it."""

    contract_size: float
    """Units per contract.

    For a linear contract this is an amount of the base asset (5 BTC for a CME
    Bitcoin future). For an inverse contract it is an amount of the quote
    currency (100 USD for a BitMEX-style inverse). Spot is always 1.0.
    """

    linear: bool
    """Whether the contract settles linearly.

    Linear contracts are denominated in the base asset, so their quote-currency
    notional scales with price. Inverse contracts are a fixed amount of quote
    currency and their notional does not. Getting this backwards misprices
    exposure by roughly the price itself.
    """

    quote_currency: str
    """Currency the instrument is priced in, and that notional comes out in."""

    settle_currency: str
    """Currency the contract settles in. Differs from ``quote`` when inverse."""

    tick_size: float | None
    """Minimum price increment, when the venue publishes one."""

    is_derivative: bool
    """Whether this is a derivative rather than spot."""

    def __post_init__(self) -> None:
        """Validate the spec, refusing anything that cannot be trusted.

        Raises:
            ContractMetadataError: If the size or quote currency is unusable.
        """
        _require_positive(self.contract_size, field="contract_size", symbol=self.symbol)
        if not str(self.quote_currency).strip():
            raise ContractMetadataError(f"{self.symbol}: quote currency is unknown")

    def notional(self, quantity: float, price: float) -> float:
        """Return the quote-currency exposure of ``quantity`` at ``price``.

        The linear/inverse split is the reason this is a method rather than a
        bare multiplier. A linear contract's value scales with price; an
        inverse contract is already denominated in quote currency and does
        not, so its notional is price-independent. Applying the linear formula
        to an inverse contract inflates exposure by the price -- four or five
        orders of magnitude for a crypto pair.

        Args:
            quantity: Number of contracts (or units of base, for spot).
            price: Current price in the quote currency.

        Returns:
            Exposure in the quote currency.

        Raises:
            ContractMetadataError: If quantity or price is not usable, rather
                than silently returning a number computed from a NaN.
        """
        qty = _require_positive(quantity, field="quantity", symbol=self.symbol)
        mark = _require_positive(price, field="price", symbol=self.symbol)
        if self.linear:
            return qty * self.contract_size * mark
        return qty * self.contract_size

    def describe(self) -> str:
        """Return a one-line human description, for operator-facing errors.

        Names the numbers that would otherwise be silently wrong, so an
        operator reading a refusal can tell whether the venue's metadata or
        their expectation is the thing at fault.
        """
        kind = "linear" if self.linear else "inverse"
        tick = f", tick {self.tick_size}" if self.tick_size else ""
        return (
            f"{self.symbol}: {self.contract_size} per contract, {kind}, "
            f"quoted in {self.quote_currency}{tick}"
        )


def spot_spec(symbol: str, *, quote_currency: str) -> ContractSpec:
    """Build the spec for a spot instrument.

    Spot needs no venue lookup: one unit is one unit of the base asset by
    definition, which is the one case where a multiplier of 1.0 is a fact
    rather than a guess.

    Args:
        symbol: Instrument symbol.
        quote_currency: Currency the instrument is priced in.

    Returns:
        A validated spot spec.
    """
    return ContractSpec(
        symbol=symbol,
        contract_size=1.0,
        linear=True,
        quote_currency=quote_currency,
        settle_currency=quote_currency,
        tick_size=None,
        is_derivative=False,
    )
