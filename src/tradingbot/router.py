from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from .models import Action, Order, OrderResult, Side, Signal
from .venues.base import ExecutionVenue

if TYPE_CHECKING:
    from .service.risk import GlobalExposure


class SignalRouter:
    def __init__(self, venue: ExecutionVenue) -> None:
        self._venue = venue

    @classmethod
    def with_risk_guard(
        cls,
        venue: ExecutionVenue,
        *,
        per_bot_cap: float,
        global_cap: float,
        global_state: "GlobalExposure",
        price_source: Callable[[], float | None],
        multiplier: float = 1.0,
    ) -> "SignalRouter":
        """Build a router whose orders are gated by ``RiskGuard``.

        The deferred import keeps the core router usable without importing the
        service package, while providing one explicit production wiring point
        for supervised bots.
        """
        from .service.risk import RiskGuard

        return cls(
            RiskGuard(
                venue,
                per_bot_cap=per_bot_cap,
                global_cap=global_cap,
                global_state=global_state,
                price_source=price_source,
                multiplier=multiplier,
            )
        )

    def route(self, signal: Signal) -> OrderResult:
        if signal.action is Action.close:
            return self._venue.close_position(signal.symbol)

        if signal.action is Action.buy:
            side = Side.buy
        elif signal.action is Action.sell:
            side = Side.sell
        else:
            raise ValueError(f"Unsupported action: {signal.action}")

        order = Order(
            symbol=signal.symbol,
            side=side,
            order_type=signal.order_type,
            qty=signal.quantity,
            price=signal.price,
        )
        return self._venue.place_order(order)
