"""What a spot bot actually owns, derived from its own fills.

``CcxtVenue.get_position()`` reports the whole account's base-asset balance as
the calling bot's position, with ``entry_price=0.0``. Two consequences, both
bad:

- **A bot could sell inventory it never bought.** ``close_position()`` sells
  the reported size, so a bot could liquidate coins bought by hand, or another
  bot's position on the same account.
- **Reported PnL was the holding's market value, not profit.** With an entry
  price of zero, ``(mark - entry) * size`` is just ``mark * size``.

An exchange balance genuinely cannot answer "what does this bot own" -- it is
one number for the whole account and carries no attribution. The only source
that can is the bot's own fills, which #135 made durable. So ownership is
derived here rather than asked for.

**Average-cost, not FIFO.** Both are defensible; average cost is chosen
because it needs no lot queue to stay correct across restarts (the position is
recomputed from the order log every time, and a moving average is a pure fold
where FIFO is not), and because the issue asks for an "average cost" figure
directly. The difference only shows in *realized* PnL when lots were bought at
different prices; unrealized and quantity are identical either way.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from .ledger import OrderRecord

_QTY_EPSILON = 1e-9
"""Below this a position is treated as closed; see ledger._QTY_EPSILON."""


@dataclass(frozen=True)
class SpotPosition:
    """A bot's spot holding in one symbol, from its own fills."""

    quantity: float
    """Units the bot owns. Never negative: spot cannot be short."""

    average_cost: float
    """Quantity-weighted price paid for what is still held. 0 when flat."""

    realized_pnl: float
    """Profit banked by sales, net of fees."""

    @property
    def is_flat(self) -> bool:
        """Whether the bot holds nothing."""
        return self.quantity <= _QTY_EPSILON

    def unrealized_pnl(self, mark_price: float) -> float:
        """Return profit on the held quantity at ``mark_price``.

        Uses the persisted cost basis. The old behaviour marked against an
        entry price of zero, which reported the holding's whole market value
        as profit.

        Args:
            mark_price: Current price in the quote currency.

        Returns:
            Unrealized profit, or ``0.0`` when flat or the price is unusable.
        """
        if self.is_flat or not math.isfinite(mark_price) or mark_price <= 0:
            return 0.0
        return (mark_price - self.average_cost) * self.quantity


def spot_position(orders: Iterable["OrderRecord"]) -> SpotPosition:
    """Fold a bot's orders into what it owns and what it has banked.

    Only filled quantity counts: a submission, a dry run and a rejection all
    contribute nothing, because ownership needs fill evidence (#135).

    Orders are applied in time order regardless of the order they arrive in,
    since realized PnL depends on what the basis was when each sale happened.

    Args:
        orders: This bot's orders. Scope them with
            ``ledger.orders(bot_id=...)`` -- passing another bot's orders is
            exactly the account-wide attribution this module exists to remove.

    Returns:
        The bot's spot position.
    """
    quantity = 0.0
    cost_basis = 0.0
    """Total cost of the held quantity, kept alongside so the average
    survives partial disposals without recomputing from history."""
    realized = 0.0

    for order in sorted(orders, key=lambda o: (o.updated_ts, o.created_ts)):
        filled = order.filled_qty
        if filled <= _QTY_EPSILON or order.avg_price <= 0:
            continue

        if order.side == "buy":
            quantity += filled
            cost_basis += filled * order.avg_price
        elif order.side == "sell":
            # A sale can only dispose of what is actually held. Selling more
            # than that is a reporting error or a manual trade on the same
            # account; crediting profit for the excess against a zero basis
            # would invent money.
            sold = min(filled, quantity)
            if sold > _QTY_EPSILON:
                average = cost_basis / quantity if quantity > _QTY_EPSILON else 0.0
                realized += sold * (order.avg_price - average)
                quantity -= sold
                cost_basis -= sold * average
        else:
            continue

        # Fees are a cost either way round, so they reduce banked profit
        # rather than adjusting the basis of what is still held.
        realized -= order.fees

        if quantity <= _QTY_EPSILON:
            # Fully exited: drop the residue so a later buy starts clean
            # rather than inheriting float dust as its cost.
            quantity = 0.0
            cost_basis = 0.0

    average_cost = cost_basis / quantity if quantity > _QTY_EPSILON else 0.0
    return SpotPosition(
        quantity=quantity, average_cost=average_cost, realized_pnl=realized
    )
