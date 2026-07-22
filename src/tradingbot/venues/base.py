"""Execution venue protocol abstracting broker/exchange implementations."""

from typing import Protocol

from ..models import Order, OrderResult, Position


class ExecutionVenue(Protocol):
    """Protocol for venues that can execute orders and report positions."""

    def place_order(self, order: Order) -> OrderResult:
        """Submit an order to the venue.

        Args:
            order: Order details including symbol, side, quantity and type.

        Returns:
            Result describing fill state or failure.
        """
        ...

    def close_position(self, symbol: str) -> OrderResult:
        """Close any open position for ``symbol``.

        Implementations may additionally accept an ``owned_qty`` keyword so a
        spot bot closes only what it bought rather than the whole account
        balance (#128). That is deliberately not required here: like
        ``contract_spec`` and ``fetch_order``, it is an optional capability
        the caller detects, so a venue that cannot support it stays a valid
        ``ExecutionVenue``.

        Args:
            symbol: Trading symbol to flatten.

        Returns:
            Result of the closing order.
        """
        ...

    def get_position(self, symbol: str) -> Position | None:
        """Return the current position for ``symbol`` if one exists.

        Args:
            symbol: Trading symbol to query.

        Returns:
            The current position, or ``None`` when flat.
        """
        ...

    def health_check(self) -> bool:
        """Return ``True`` when the venue is reachable and healthy."""
        ...
