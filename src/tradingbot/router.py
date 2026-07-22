"""Route strategy signals to execution venues, optionally through risk guards."""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from .models import Action, Order, OrderResult, Side, Signal
from .venues.base import ExecutionVenue
from .venues.capabilities import CapabilityError, VenueCapabilities, check_signal

if TYPE_CHECKING:
    from .service.exposure import ExposureTracker
    from .venues.contracts import ContractSpec


def _accepts(func: object, name: str) -> bool:
    """Return whether ``func`` can be called with keyword ``name``.

    Optional venue capabilities are detected rather than mandated, so a venue
    that predates a keyword stays a valid ``ExecutionVenue``.
    """
    try:
        params = inspect.signature(func).parameters  # type: ignore[arg-type]
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        return False
    return name in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


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
        capabilities: "VenueCapabilities | None" = None,
    ) -> None:
        """Create a router backed by ``venue``.

        Args:
            venue: Venue used to place orders and close positions.
            owned_qty_source: Returns how much of the symbol this bot owns,
                for closes (#128). Spot venues can only see the whole
                account's balance, so without this a close would sell coins
                bought by hand or by another bot. ``None`` leaves the venue to
                report its own position, which is right for derivatives.
            capabilities: What the venue can do (#125). Signals it cannot
                support -- a short on spot, or an action that contradicts its
                own declared position side -- are refused here rather than
                sent. ``None`` disables the check, which is the behaviour
                every caller had before capabilities existed.
        """
        self._venue = venue
        self._owned_qty_source = owned_qty_source
        self._capabilities = capabilities

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
        capabilities: "VenueCapabilities | None" = None,
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
            capabilities=capabilities,
        )

    def _close_outcome(self, symbol: str) -> RouteOutcome:
        """Close ``symbol`` and report the order the venue built for it.

        A close constructs its order inside the venue, so until #121 there was
        nothing to record it against and closes never reached the ledger.
        The key is stamped here, and the venue echoes back the order it
        actually sent so the caller records what happened rather than a guess.

        A venue that reports no closing order yields ``None``, exactly as
        before -- older venues must not break.
        """
        client_order_id = uuid.uuid4().hex
        result = self._close(symbol, client_order_id=client_order_id)
        raw = result.raw if isinstance(result.raw, dict) else {}
        reported = raw.get("closing_order")
        if not isinstance(reported, dict):
            return RouteOutcome(None, result)
        try:
            order = Order.model_validate({**reported, "client_order_id": client_order_id})
        except Exception:  # pragma: no cover - defensive; a malformed echo
            return RouteOutcome(None, result)
        return RouteOutcome(order, result)

    def _close(self, symbol: str, *, client_order_id: str | None = None) -> OrderResult:
        """Close ``symbol``, telling the venue what this bot owns if it can use it.

        ``owned_qty`` is an optional venue capability rather than part of the
        ``ExecutionVenue`` protocol, so it is detected: requiring it would
        force every venue and every test double to accept an argument most of
        them cannot use.
        """
        close = cast(Any, self._venue.close_position)
        extras: dict[str, object] = {}
        if client_order_id is not None and _accepts(close, "client_order_id"):
            extras["client_order_id"] = client_order_id
        if self._owned_qty_source is not None and _accepts(close, "owned_qty"):
            extras["owned_qty"] = self._owned_qty_source()
        return close(symbol, **extras)

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
        if self._capabilities is not None:
            try:
                check_signal(signal, self._capabilities)
            except CapabilityError as exc:
                # Refused before the venue is touched, so an unsupported
                # pairing costs nothing and says what to change.
                return RouteOutcome(None, OrderResult(
                    ok=False, order_id=None, status="incompatible",
                    filled_qty=0.0, raw={}, error=str(exc),
                ))

        if signal.action is Action.close:
            return self._close_outcome(signal.symbol)

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
