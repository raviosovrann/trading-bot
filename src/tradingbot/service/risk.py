from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from ..models import Order, OrderResult, Position
from ..venues.base import ExecutionVenue


@dataclass
class GlobalExposure:
    """Shared notional exposure across all supervised bots."""

    used: float = 0.0


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
        multiplier: float = 1.0,
    ) -> None:
        self._venue = venue
        self._per_bot_cap = per_bot_cap
        self._global_cap = global_cap
        self._global_state = global_state
        self._price_source = price_source
        self._multiplier = multiplier

    def place_order(self, order: Order) -> OrderResult:
        if order.reduce_only:
            result = self._place(order)
            if result.ok and self._has_positive_fill(result.filled_qty):
                self._decrease_exposure(order, result.filled_qty)
            return result

        price = self._get_price()
        if price is None or not self._valid_order_size(order):
            return self._blocked(0.0, error="price or order size unavailable")

        notional = order.qty * price * self._multiplier
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
        return self._venue.close_position(symbol)

    def get_position(self, symbol: str) -> Position | None:
        return self._venue.get_position(symbol)

    def health_check(self) -> bool:
        return self._venue.health_check()

    def _place(self, order: Order) -> OrderResult:
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
        price = self._get_price()
        if price is None or not self._valid_order_size(order, qty=filled_qty):
            return
        notional = filled_qty * price * self._multiplier
        self._global_state.used = max(0.0, self._global_state.used - notional)

    def _valid_order_size(self, order: Order, *, qty: float | None = None) -> bool:
        size = order.qty if qty is None else qty
        return (
            math.isfinite(size)
            and size > 0
            and math.isfinite(self._multiplier)
            and self._multiplier > 0
        )

    @staticmethod
    def _has_positive_fill(filled_qty: float) -> bool:
        return math.isfinite(filled_qty) and filled_qty > 0

    def _get_price(self) -> float | None:
        try:
            price = self._price_source()
        except Exception:
            return None
        if price is None or not math.isfinite(price) or price <= 0:
            return None
        return price

    @staticmethod
    def _blocked(notional: float, *, error: str) -> OrderResult:
        return OrderResult(
            ok=False,
            order_id=None,
            status="risk_blocked",
            filled_qty=0.0,
            raw={"notional": notional},
            error=error,
        )