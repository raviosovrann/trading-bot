from __future__ import annotations

from .models import Action, Order, OrderResult, Side, Signal
from .venues.base import ExecutionVenue


class SignalRouter:
    def __init__(self, venue: ExecutionVenue) -> None:
        self._venue = venue

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
