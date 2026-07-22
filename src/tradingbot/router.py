"""Route strategy signals to execution venues, optionally through risk guards."""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .models import Action, Order, OrderResult, Side, Signal
from .venues.base import ExecutionVenue

if TYPE_CHECKING:
    from .service.exposure import ExposureTracker
    from .venues.contracts import ContractSpec


@dataclass(frozen=True)
class RouteOutcome:
    """What a routed signal actually sent, and what came back.

    The result alone is not enough to record an order durably: the ledger keys
    on the client order id, which lives on the submitted ``Order``. A ``close``
    action goes through ``close_position()`` and constructs no order, so
    ``order`` is ``None`` there.
    """

    order: Order | None
    result: OrderResult


class SignalRouter:
    """Convert ``Signal`` objects into venue orders."""

    def __init__(
        self,
        venue: ExecutionVenue,
        *,
        owned_qty_source: Callable[[], float] | None = None,
    ) -> None:
        """Create a router backed by ``venue``.

        Args:
            venue: Venue used to place orders and close positions.
            owned_qty_source: Returns how much of the symbol this bot owns,
                for closes (#128). Spot venues can only see the whole
                account's balance, so without this a close would sell coins
                bought by hand or by another bot. ``None`` leaves the venue to
                report its own position, which is right for derivatives.
        """
        self._venue = venue
        self._owned_qty_source = owned_qty_source

    @classmethod
    def with_risk_guard(
        cls,
        venue: ExecutionVenue,
        *,
        per_bot_cap: float,
        global_cap: float,
        exposure: "ExposureTracker",
        bot_id: str = "",
        price_source: Callable[[], float | None],
        contract: "ContractSpec | None" = None,
        owned_qty_source: Callable[[], float] | None = None,
    ) -> "SignalRouter":
        """Build a router whose orders are gated by ``RiskGuard``.

        The deferred import keeps the core router usable without importing the
        service package, while providing one explicit production wiring point
        for supervised bots.

        Args:
            venue: Underlying execution venue.
            per_bot_cap: Maximum notional exposure allowed for one bot.
            global_cap: Maximum notional exposure allowed across all bots.
            exposure: Shared per-bot and global exposure tracker (#110).
            bot_id: Bot the orders belong to, for per-bot attribution.
            price_source: Callable returning the latest price for notional checks.
            contract: Resolved contract metadata used to compute notional
                (#124); linear and inverse contracts price differently.

        Returns:
            A router wrapped with per-bot and global risk limits.
        """
        from .service.risk import RiskGuard

        return cls(
            RiskGuard(
                venue,
                per_bot_cap=per_bot_cap,
                global_cap=global_cap,
                exposure=exposure,
                bot_id=bot_id,
                price_source=price_source,
                contract=contract,
            ),
            owned_qty_source=owned_qty_source,
        )

    def _close(self, symbol: str) -> OrderResult:
        """Close ``symbol``, telling the venue what this bot owns if it can use it.

        ``owned_qty`` is an optional venue capability rather than part of the
        ``ExecutionVenue`` protocol, so it is detected: requiring it would
        force every venue and every test double to accept an argument most of
        them cannot use.
        """
        close = self._venue.close_position
        if self._owned_qty_source is None:
            return close(symbol)
        try:
            params = inspect.signature(close).parameters
            accepts = "owned_qty" in params or any(
                # A venue taking **kwargs can receive it too.
                p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
        except (TypeError, ValueError):  # pragma: no cover - exotic callables
            accepts = False
        if not accepts:
            return close(symbol)
        return close(symbol, owned_qty=self._owned_qty_source())  # type: ignore[call-arg]

    def route(self, signal: Signal) -> OrderResult:
        """Translate ``signal`` into an order and send it to the venue.

        Args:
            signal: Strategy signal to execute.

        Returns:
            The venue's order result.

        Raises:
            ValueError: If the signal action is not supported.
        """
        return self.route_detailed(signal).result

    def route_detailed(self, signal: Signal) -> RouteOutcome:
        """Route ``signal`` and report the submitted order alongside the result.

        Callers that persist orders need both halves: the ledger records what
        was sent, keyed on the client order id, and only then folds in what the
        venue said about it.

        Args:
            signal: Strategy signal to execute.

        Returns:
            The submitted order (``None`` for a close) and the venue's result.

        Raises:
            ValueError: If the signal action is not supported.
        """
        if signal.action is Action.close:
            return RouteOutcome(None, self._close(signal.symbol))

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
            # Stamped here, before the venue is touched, so an order that is
            # submitted but never acknowledged is still identifiable (#135).
            client_order_id=uuid.uuid4().hex,
        )
        return RouteOutcome(order, self._venue.place_order(order))
