"""What a venue can do, what a strategy needs, and whether they agree.

Strategies and venues were registered as flat lists of names, so any strategy
could be paired with any venue and nothing checked the combination. Two things
went wrong as a result:

- **Spot is long-only, but nothing said so.** A short-capable strategy on a
  spot venue would either be rejected by the exchange at submission time or,
  worse, express its "short" by selling inventory it happened to hold -- a
  different trade from the one it intended.
- **``SignalRouter`` never read ``Signal.position_side``.** It mapped a sell
  action straight to a sell order, so a signal whose action and declared
  position intent disagreed was executed on the action alone.

Both are checked here instead, before anything is submitted. A capability
mismatch is a configuration error the operator can fix, so it fails loudly at
create and start time rather than becoming a runtime surprise on the first
signal that happens to need the missing feature.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Action, OrderType, PositionSide, Signal


class CapabilityError(Exception):
    """Raised when a venue cannot support what is being asked of it."""


@dataclass(frozen=True)
class VenueCapabilities:
    """What one venue/market-type pair can actually do."""

    venue: str
    market_type: str

    supports_short: bool
    """Whether a negative position can be held.

    False for spot: selling there disposes of inventory rather than opening a
    short, so the two are not interchangeable even though both send a sell.
    """

    supports_reduce_only: bool
    """Whether the venue can guarantee an order only reduces a position."""

    order_types: frozenset[OrderType]
    """Order types the venue accepts."""

    def describe(self) -> str:
        """Return a one-line human description, for operator-facing errors."""
        traits = ["short" if self.supports_short else "long-only"]
        if self.supports_reduce_only:
            traits.append("reduce-only")
        types = ", ".join(sorted(t.value for t in self.order_types))
        return f"{self.venue}/{self.market_type} ({', '.join(traits)}; {types})"


@dataclass(frozen=True)
class StrategyRequirements:
    """What a strategy needs from whatever venue it is pointed at.

    The default requires nothing, so an existing strategy stays valid
    everywhere until it declares otherwise. Requirements are opt-in because a
    strategy that never shorts should not be barred from spot for lack of a
    declaration.
    """

    requires_short: bool = False
    requires_reduce_only: bool = False
    required_order_types: frozenset[OrderType] = field(default_factory=frozenset)


def check_strategy(
    name: str, requirements: StrategyRequirements, venue: VenueCapabilities
) -> None:
    """Verify ``venue`` can support ``requirements``.

    Args:
        name: Strategy name, for the error message.
        requirements: What the strategy needs.
        venue: Capabilities of the selected venue/market pair.

    Raises:
        CapabilityError: If the venue cannot meet a requirement. The message
            names the strategy and the venue, because the operator's fix is to
            change one of the two.
    """
    if requirements.requires_short and not venue.supports_short:
        raise CapabilityError(
            f"strategy {name!r} needs to hold short positions, which "
            f"{venue.describe()} cannot"
        )
    if requirements.requires_reduce_only and not venue.supports_reduce_only:
        raise CapabilityError(
            f"strategy {name!r} needs reduce-only orders, which "
            f"{venue.describe()} cannot guarantee"
        )
    missing = requirements.required_order_types - venue.order_types
    if missing:
        names = ", ".join(sorted(t.value for t in missing))
        raise CapabilityError(
            f"strategy {name!r} needs {names} orders, which "
            f"{venue.describe()} does not accept"
        )


def check_signal(signal: Signal, venue: VenueCapabilities) -> None:
    """Verify a signal is coherent and executable on ``venue``.

    Two separate checks. The first is whether the venue can express the
    position the signal is reaching for at all. The second is whether the
    signal is internally consistent: buying toward a short, or selling toward
    a long, describes a trade that cannot be what the strategy meant, and
    executing it on the action alone -- as the router used to -- silently
    picks one half of a contradiction.

    ``close`` is exempt from **every** check. What a close does is determined
    by the position actually held, not by a declared side or an order type,
    and refusing one is the dangerous direction: a blocked close strands a
    position the strategy is trying to get out of. A close only ever reduces
    risk, so there is nothing here worth protecting against.

    Args:
        signal: The strategy's signal.
        venue: Capabilities of the venue it would be routed to.

    Raises:
        CapabilityError: If the signal cannot or should not be executed.
    """
    if signal.action is Action.close:
        return

    if signal.order_type not in venue.order_types:
        raise CapabilityError(
            f"{signal.order_type.value} orders are not accepted by "
            f"{venue.describe()}"
        )

    if signal.position_side is PositionSide.short and not venue.supports_short:
        raise CapabilityError(
            f"signal targets a short position, which {venue.describe()} "
            "cannot hold; on spot a sell disposes of inventory instead"
        )

    if signal.action is Action.buy and signal.position_side is PositionSide.short:
        raise CapabilityError(
            "incoherent signal: a buy cannot open a short position"
        )
    if signal.action is Action.sell and signal.position_side is PositionSide.long:
        raise CapabilityError(
            "incoherent signal: a sell cannot open a long position"
        )
