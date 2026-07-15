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
