"""Risk guard that enforces per-bot and global notional limits."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from ..models import Order, OrderResult, Position
from ..venues.base import ExecutionVenue
from ..venues.contracts import ContractMetadataError, ContractSpec, spot_spec


@dataclass
class GlobalExposure:
    """Shared notional exposure across all supervised bots."""

    used: float = 0.0
    """Total notional exposure currently used by all bots."""


class RiskGuard:
    """Apply per-bot and global notional limits to an execution venue."""

    def __init__(
        self,
        venue: ExecutionVenue,
        *,
        per_bot_cap: float,
        global_cap: float,
        global_state: GlobalExposure,
        price_source: Callable[[], float | None],
        contract: ContractSpec | None = None,
    ) -> None:
        """Wrap ``venue`` with notional risk checks.

        Args:
            venue: Underlying execution venue.
            per_bot_cap: Maximum notional exposure for one bot.
            global_cap: Maximum notional exposure across all bots.
            global_state: Shared exposure tracker.
            price_source: Callable returning the current price for notional checks.
            contract: Resolved contract metadata (#124). Notional comes from
                the spec rather than a bare multiplier, because a multiplier
                can only express the linear convention: an inverse contract is
                a fixed amount of quote currency, and pricing one linearly
                inflates its exposure by the price itself. Defaults to a spot
                unit contract, where the two conventions agree.
        """
        self._venue = venue
        self._per_bot_cap = per_bot_cap
        self._global_cap = global_cap
        self._global_state = global_state
        self._price_source = price_source
        self._contract = contract or spot_spec("", quote_currency="?")

    def place_order(self, order: Order) -> OrderResult:
        """Place ``order`` if it passes notional caps.

        Args:
            order: Order to submit.

        Returns:
            Venue result, or a risk-blocked result if limits are exceeded.
        """
        if order.reduce_only:
            result = self._place(order)
            if result.ok and self._has_positive_fill(result.filled_qty):
                self._decrease_exposure(order, result.filled_qty)
            return result

        price = self._get_price()
        if price is None or not self._valid_order_size(order):
            return self._blocked(0.0, error="price or order size unavailable")

        notional = self._notional(order.qty, price)
        if notional is None:
            return self._blocked(0.0, error="exposure could not be computed")
        if (
            notional > self._per_bot_cap
            or self._global_state.used + notional > self._global_cap
        ):
            return self._blocked(notional, error="notional cap exceeded")

        result = self._place(order)
        if result.ok:
            self._global_state.used += notional
        return result

    def close_position(self, symbol: str) -> OrderResult:
        """Close the position for ``symbol`` on the underlying venue.

        Args:
            symbol: Trading symbol to close.

        Returns:
            Result of the closing order.
        """
        return self._venue.close_position(symbol)

    def get_position(self, symbol: str) -> Position | None:
        """Return the current position for ``symbol``.

        Args:
            symbol: Trading symbol to query.

        Returns:
            The current position, or ``None`` when flat.
        """
        return self._venue.get_position(symbol)

    def health_check(self) -> bool:
        """Return the health status of the underlying venue."""
        return self._venue.health_check()

    def _place(self, order: Order) -> OrderResult:
        """Place ``order`` and convert exceptions into failed results.

        Args:
            order: Order to submit.

        Returns:
            Venue result, or a failed result if the venue raised an exception.
        """
        try:
            return self._venue.place_order(order)
        except Exception as exc:
            return OrderResult(
                ok=False,
                order_id=None,
                status="error",
                filled_qty=0.0,
                raw={},
                error=str(exc),
            )

    def _decrease_exposure(self, order: Order, filled_qty: float) -> None:
        """Reduce global exposure after a positive reduce-only fill.

        Args:
            order: Order that reduced the position.
            filled_qty: Filled quantity to translate into notional reduction.
        """
        price = self._get_price()
        if price is None or not self._valid_order_size(order, qty=filled_qty):
            return
        notional = self._notional(filled_qty, price)
        if notional is None:
            return
        self._global_state.used = max(0.0, self._global_state.used - notional)

    def _notional(self, quantity: float, price: float) -> float | None:
        """Return the quote-currency exposure, or ``None`` if it is unknowable.

        Delegates to the contract spec so linear and inverse conventions are
        each priced their own way. Returns ``None`` rather than a guess when
        the inputs are unusable -- the caller blocks the order, since an
        exposure that cannot be computed cannot be checked against a cap.
        """
        try:
            return self._contract.notional(quantity, price)
        except ContractMetadataError:
            return None

    def _valid_order_size(self, order: Order, *, qty: float | None = None) -> bool:
        """Return ``True`` when the order size is usable.

        The contract size no longer needs checking here: ContractSpec
        validates it on construction (#124), so an unusable one cannot reach
        this point.

        Args:
            order: Order to validate.
            qty: Optional quantity override; defaults to ``order.qty``.

        Returns:
            ``True`` if the size is finite and positive.
        """
        size = order.qty if qty is None else qty
        return math.isfinite(size) and size > 0

    @staticmethod
    def _has_positive_fill(filled_qty: float) -> bool:
        """Return ``True`` if ``filled_qty`` is a positive finite number."""
        return math.isfinite(filled_qty) and filled_qty > 0

    def _get_price(self) -> float | None:
        """Return a validated price from the price source.

        Returns:
            A positive finite price, or ``None`` if unavailable or invalid.
        """
        try:
            price = self._price_source()
        except Exception:
            return None
        if price is None or not math.isfinite(price) or price <= 0:
            return None
        return price

    @staticmethod
    def _blocked(notional: float, *, error: str) -> OrderResult:
        """Build a risk-blocked result.

        Args:
            notional: Notional value that triggered the block.
            error: Human-readable block reason.

        Returns:
            An ``OrderResult`` with ``ok=False`` and ``status="risk_blocked"``.
        """
        return OrderResult(
            ok=False,
            order_id=None,
            status="risk_blocked",
            filled_qty=0.0,
            raw={"notional": notional},
            error=error,
        )
