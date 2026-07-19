"""Durable order and execution records, and the projection that folds them.

The system used to infer what a bot had done from whatever the venue returned
at submission time. That is not enough: Tradovate acknowledges an order before
any fill exists, so a submission response says nothing about the resulting
position. Everything numeric downstream -- exposure caps (#110) and spot cost
basis (#128) -- has to be computed from *fill evidence* instead.

So the durable form is an append-only log of lifecycle events, and this module
is the projection that folds it into current state. Event sourcing rather than
mutable rows, for three reasons that all come from how venues actually behave:

- **They replay.** A reconnect re-delivers events already seen, so application
  has to be idempotent. Fills carry a venue execution id and are deduplicated
  on it.
- **They arrive out of order.** A fill can reach us before the submission
  acknowledgement it belongs to, and after a cancel. Folding tolerates both
  rather than assuming a sequence.
- **Restart has to recover.** Replaying the log rebuilds exact state, which is
  what makes reconciliation of open orders after a restart tractable.

An explicit terminal event always wins over the state implied by fills: an
order the venue told us was canceled stays canceled even if a straggling fill
lands afterwards. The fill still counts toward the position -- it happened --
but it cannot reopen the order.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from ..models import Order, OrderResult

_QTY_EPSILON = 1e-9
"""Quantities closer than this are considered equal.

Venue-reported fill quantities are floats and rarely sum to the requested
quantity exactly, so a fill that completes an order can leave a remainder of
the order of 1e-12. Treating that as still-open would leave the order live
forever and hold its exposure reservation with it.
"""


class OrderState(str, Enum):
    """Lifecycle state of a single order.

    Deliberately distinct from the old free-form ``status`` string, which
    conflated "the venue accepted this" with "this traded".
    """

    dry_run = "dry_run"
    """Never sent to the venue. Carries no exposure and no position."""

    submitted = "submitted"
    """Acknowledged by the venue, no fill evidence yet."""

    partially_filled = "partially_filled"
    """Some quantity has traded; the remainder is still live."""

    filled = "filled"
    """The full requested quantity has traded."""

    canceled = "canceled"
    """Withdrawn. May still carry fills that landed before the cancel."""

    rejected = "rejected"
    """Refused by the venue. Never carries fills."""


TERMINAL_STATES = frozenset({
    OrderState.dry_run,
    OrderState.filled,
    OrderState.canceled,
    OrderState.rejected,
})
"""States from which no further progress is expected.

Exposure reservations are released on entry to any of these, and
reconciliation after a restart only has to chase orders outside this set.
"""

_EXPLICIT_TERMINAL_KINDS = {
    "dry_run": OrderState.dry_run,
    "canceled": OrderState.canceled,
    "rejected": OrderState.rejected,
}


@dataclass(frozen=True)
class Execution:
    """A single fill: evidence that quantity actually traded.

    Identity is ``(client_order_id, exec_id)``. ``exec_id`` comes from the
    venue where one is available, which is what makes replayed events
    harmless.
    """

    exec_id: str
    client_order_id: str
    bot_id: str
    symbol: str
    side: str
    qty: float
    price: float
    fee: float
    fee_currency: str
    ts: int


@dataclass(frozen=True)
class OrderRecord:
    """Current projected state of one order.

    Immutable: it is a snapshot of the fold at read time, not a mutable row.
    Callers that hold one across further ``apply()`` calls are holding history,
    which is usually what they want.
    """

    client_order_id: str
    bot_id: str
    symbol: str
    side: str
    order_type: str
    qty: float
    price: float | None
    venue_order_id: str | None
    state: OrderState
    filled_qty: float
    avg_price: float
    fees: float
    created_ts: int
    updated_ts: int
    error: str | None = None

    @property
    def remaining_qty(self) -> float:
        """Quantity still live, floored at zero.

        Overfills -- a venue reporting more filled than requested -- are
        clamped rather than reported as negative remaining.
        """
        return max(0.0, self.qty - self.filled_qty)

    @property
    def is_terminal(self) -> bool:
        """Whether no further progress is expected on this order."""
        return self.state in TERMINAL_STATES


@dataclass
class _MutableOrder:
    """Internal accumulator. Converted to a frozen ``OrderRecord`` on read."""

    client_order_id: str
    bot_id: str = ""
    symbol: str = ""
    side: str = ""
    order_type: str = ""
    qty: float = 0.0
    price: float | None = None
    venue_order_id: str | None = None
    submitted_seen: bool = False
    terminal: OrderState | None = None
    fills_qty: float = 0.0
    """Quantity summed from discrete execution events."""
    fills_notional: float = 0.0
    """Quantity-weighted notional of the discrete executions."""
    snapshot_qty: float = 0.0
    """Cumulative filled quantity last reported by an order-status snapshot."""
    snapshot_avg: float = 0.0
    """Average fill price reported alongside ``snapshot_qty``."""
    fees: float = 0.0
    created_ts: int = 0
    updated_ts: int = 0
    error: str | None = None

    @property
    def filled_qty(self) -> float:
        """Filled quantity, reconciled across both reporting styles.

        Venues report progress two ways and some do both: discrete executions
        pushed as they happen, and cumulative snapshots on the submission
        response or an order-status poll. Summing them double-counts the same
        quantity. Taking the greater believes whichever source is further
        ahead, which errs toward overstating what is at risk -- the safe
        direction, since the alternative is releasing exposure for a position
        that is still open.
        """
        return max(self.fills_qty, self.snapshot_qty)

    @property
    def qty_known(self) -> bool:
        """Whether the requested quantity is known.

        False for an order first seen through one of its own fills: we know
        something traded but not how much was asked for, so completion cannot
        be judged yet.
        """
        return self.qty > _QTY_EPSILON

    def state(self) -> OrderState:
        """Fold the accumulated evidence into a single state."""
        if self.terminal is not None:
            return self.terminal
        if self.qty_known and self.filled_qty >= self.qty - _QTY_EPSILON:
            return OrderState.filled
        if self.filled_qty > _QTY_EPSILON:
            return OrderState.partially_filled
        return OrderState.submitted

    def avg_price(self) -> float:
        """Average fill price, from whichever source is further ahead.

        Discrete executions carry exact per-fill prices, so they are preferred
        when they account for at least as much quantity as the snapshot does.
        A venue's snapshot average is rounded and, when the snapshot leads, is
        the only evidence available for the quantity the fills have not
        covered yet.
        """
        if self.fills_qty > _QTY_EPSILON and self.fills_qty >= self.snapshot_qty:
            return self.fills_notional / self.fills_qty
        return self.snapshot_avg if self.snapshot_qty > _QTY_EPSILON else 0.0

    def snapshot(self) -> OrderRecord:
        """Build the immutable record for this order's current state."""
        return OrderRecord(
            client_order_id=self.client_order_id,
            bot_id=self.bot_id,
            symbol=self.symbol,
            side=self.side,
            order_type=self.order_type,
            qty=self.qty,
            price=self.price,
            venue_order_id=self.venue_order_id,
            state=self.state(),
            filled_qty=self.filled_qty,
            avg_price=self.avg_price(),
            fees=self.fees,
            created_ts=self.created_ts,
            updated_ts=self.updated_ts,
            error=self.error,
        )


@dataclass(frozen=True)
class _Fill:
    """A recorded fill, before it is joined with its order for presentation."""

    exec_id: str
    client_order_id: str
    qty: float
    price: float
    fee: float
    fee_currency: str
    ts: int


def _coerce_float(value: Any) -> float | None:
    """Return ``value`` as a finite float, or ``None`` if it is not one.

    Records come off disk and off the wire, so a malformed field is expected
    rather than exceptional and must not take the projection down.
    """
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


class OrderLedger:
    """Folds order lifecycle events into current orders and fill history.

    Application is idempotent and order-independent: the same set of events
    produces the same projection regardless of arrival order or repetition.
    Rebuild after a restart by replaying the persisted log through ``apply``.
    """

    def __init__(self) -> None:
        self._orders: dict[str, _MutableOrder] = {}
        self._fills: list[_Fill] = []
        self._seen_fills: set[tuple[str, str]] = set()

    def apply(self, event: dict[str, Any]) -> bool:
        """Fold one lifecycle event into the projection.

        Args:
            event: Raw event mapping with a ``kind`` of ``submitted``,
                ``dry_run``, ``fill``, ``canceled`` or ``rejected``.

        Returns:
            ``True`` if the event changed the projection; ``False`` if it was a
            duplicate, malformed, or otherwise ignored. Callers persisting the
            log can use this to skip writing events that carry no information.
        """
        kind = str(event.get("kind", ""))
        client_order_id = event.get("client_order_id")
        if not client_order_id:
            return False
        client_order_id = str(client_order_id)

        if kind == "fill":
            return self._apply_fill(client_order_id, event)
        if kind == "order_status":
            return self._apply_status(client_order_id, event)
        if kind == "submitted":
            return self._apply_submitted(client_order_id, event)
        if kind in _EXPLICIT_TERMINAL_KINDS:
            return self._apply_terminal(client_order_id, kind, event)
        return False

    def order(self, client_order_id: str) -> OrderRecord | None:
        """Return the current state of one order, or ``None`` if unknown."""
        order = self._orders.get(client_order_id)
        return order.snapshot() if order is not None else None

    def orders(self, *, bot_id: str | None = None) -> list[OrderRecord]:
        """Return all known orders, oldest first.

        Args:
            bot_id: Restrict to one bot. Orders belong to exactly one bot, so
                this is the scoping every caller outside reconciliation wants.
        """
        return [
            order.snapshot()
            for order in self._orders.values()
            if bot_id is None or order.bot_id == bot_id
        ]

    def open_orders(self, *, bot_id: str | None = None) -> list[OrderRecord]:
        """Return orders that are not in a terminal state, oldest first.

        This is the reconciliation worklist after a restart or reconnect: these
        are the orders whose true state the venue may know better than we do.
        """
        return [order for order in self.orders(bot_id=bot_id) if not order.is_terminal]

    def executions(self, client_order_id: str | None = None) -> list[Execution]:
        """Return recorded fills in arrival order.

        Fills are joined with their order here rather than at application time,
        so a fill that arrived before its submission still reports the correct
        symbol and side once that submission lands.

        Args:
            client_order_id: Restrict to fills of a single order.
        """
        return [
            self._to_execution(fill)
            for fill in self._fills
            if client_order_id is None or fill.client_order_id == client_order_id
        ]

    def _order_for(self, client_order_id: str) -> _MutableOrder:
        """Return the accumulator for ``client_order_id``, creating it if new.

        Created on demand so a fill that outruns its own submission has
        somewhere to land.
        """
        order = self._orders.get(client_order_id)
        if order is None:
            order = _MutableOrder(client_order_id=client_order_id)
            self._orders[client_order_id] = order
        return order

    def _apply_submitted(self, client_order_id: str, event: dict[str, Any]) -> bool:
        """Record submission details, without disturbing existing progress.

        A repeated submission is common on reconnect. It refreshes the order's
        descriptive fields but must never reset fills or reopen a terminal
        order.
        """
        order = self._order_for(client_order_id)
        if order.submitted_seen:
            return False

        qty = _coerce_float(event.get("qty")) or 0.0
        order.submitted_seen = True
        order.bot_id = str(event.get("bot_id", order.bot_id))
        order.symbol = str(event.get("symbol", order.symbol))
        order.side = str(event.get("side", order.side))
        order.order_type = str(event.get("order_type", order.order_type))
        order.qty = max(0.0, qty)
        order.price = _coerce_float(event.get("price"))
        order.venue_order_id = (
            str(event["venue_order_id"]) if event.get("venue_order_id") else order.venue_order_id
        )
        ts = int(_coerce_float(event.get("ts")) or 0)
        order.created_ts = order.created_ts or ts
        order.updated_ts = max(order.updated_ts, ts)
        return True

    def _apply_terminal(
        self, client_order_id: str, kind: str, event: dict[str, Any]
    ) -> bool:
        """Mark an order terminal.

        The first terminal event wins. A venue that reports both a cancel and a
        rejection for the same order is contradicting itself; keeping the first
        is arbitrary but stable, and stability is what replay needs.
        """
        order = self._order_for(client_order_id)
        if order.terminal is not None:
            return False

        order.terminal = _EXPLICIT_TERMINAL_KINDS[kind]
        if kind == "dry_run":
            # A dry run never reached the venue, so the submission fields on
            # the event are all the description this order will ever have.
            order.submitted_seen = True
            order.bot_id = str(event.get("bot_id", order.bot_id))
            order.symbol = str(event.get("symbol", order.symbol))
            order.side = str(event.get("side", order.side))
            order.order_type = str(event.get("order_type", order.order_type))
            order.qty = max(0.0, _coerce_float(event.get("qty")) or 0.0)
            order.price = _coerce_float(event.get("price"))
        reason = event.get("reason") or event.get("error")
        if reason:
            order.error = str(reason)
        ts = int(_coerce_float(event.get("ts")) or 0)
        order.created_ts = order.created_ts or ts
        order.updated_ts = max(order.updated_ts, ts)
        return True

    def _apply_status(self, client_order_id: str, event: dict[str, Any]) -> bool:
        """Apply a cumulative order-status snapshot.

        Applied as a monotonic set rather than an increment: the venue is
        reporting the total filled so far, so a repeated poll of an unchanged
        order carries no new information and a snapshot that has gone backwards
        is stale. Believing a stale snapshot would resurrect a completed order
        and re-reserve exposure against it.
        """
        filled_qty = _coerce_float(event.get("filled_qty"))
        if filled_qty is None or filled_qty < 0:
            return False

        order = self._order_for(client_order_id)
        if filled_qty <= order.snapshot_qty + _QTY_EPSILON:
            return False

        order.snapshot_qty = filled_qty
        avg_price = _coerce_float(event.get("avg_price"))
        if avg_price is not None and avg_price > 0:
            order.snapshot_avg = avg_price
        fees = _coerce_float(event.get("fees"))
        if fees is not None and fees >= 0:
            # Snapshot fees are cumulative for the order, like the quantity.
            order.fees = max(order.fees, fees)
        ts = int(_coerce_float(event.get("ts")) or 0)
        order.created_ts = order.created_ts or ts
        order.updated_ts = max(order.updated_ts, ts)
        return True

    def _apply_fill(self, client_order_id: str, event: dict[str, Any]) -> bool:
        """Record a fill, deduplicating on the venue execution id.

        A fill is accepted even when the order is already terminal: quantity
        that traded before a cancel landed is real, and dropping it would
        understate the position.
        """
        exec_id = event.get("exec_id")
        if not exec_id:
            return False
        key = (client_order_id, str(exec_id))
        if key in self._seen_fills:
            return False

        qty = _coerce_float(event.get("qty"))
        price = _coerce_float(event.get("price"))
        if qty is None or qty <= _QTY_EPSILON or price is None or price < 0:
            return False

        fee = _coerce_float(event.get("fee")) or 0.0
        ts = int(_coerce_float(event.get("ts")) or 0)
        self._seen_fills.add(key)
        self._fills.append(_Fill(
            exec_id=str(exec_id),
            client_order_id=client_order_id,
            qty=qty,
            price=price,
            fee=fee,
            fee_currency=str(event.get("fee_currency", "")),
            ts=ts,
        ))

        order = self._order_for(client_order_id)
        order.fills_qty += qty
        order.fills_notional += qty * price
        order.fees += fee
        order.created_ts = order.created_ts or ts
        order.updated_ts = max(order.updated_ts, ts)
        return True

    def _to_execution(self, fill: _Fill) -> Execution:
        """Join a recorded fill with its order to build a presentable record."""
        order = self._orders.get(fill.client_order_id)
        return Execution(
            exec_id=fill.exec_id,
            client_order_id=fill.client_order_id,
            bot_id=order.bot_id if order else "",
            symbol=order.symbol if order else "",
            side=order.side if order else "",
            qty=fill.qty,
            price=fill.price,
            fee=fill.fee,
            fee_currency=fill.fee_currency,
            ts=fill.ts,
        )


_CANCELED_STATUSES = frozenset({"canceled", "cancelled"})
"""Venue status strings meaning the order was withdrawn.

Both spellings appear in the wild -- ccxt normalises to ``canceled`` but
individual exchanges leak ``cancelled`` through ``raw``.
"""


def events_from_result(
    order: "Order",
    result: "OrderResult",
    *,
    bot_id: str,
    ts: int,
) -> list[dict[str, Any]]:
    """Translate a venue submission response into ledger lifecycle events.

    This is where the system stops treating "the venue accepted this" as
    "this traded". An accepted order yields a ``submitted`` event and nothing
    more unless the response carries an actual filled quantity; a dry run or a
    rejection yields no fill evidence at all, whatever quantity the response
    happens to report.

    The translation is deterministic and carries no clock or random state, so
    replaying it during reconciliation produces byte-identical events that the
    ledger deduplicates rather than double-counting.

    Args:
        order: The order that was submitted; supplies the idempotency key.
        result: The venue's response.
        bot_id: Bot the order belongs to.
        ts: Timestamp to stamp the events with, from the bar that caused them.

    Returns:
        Lifecycle events to persist and fold, oldest first. Empty when
        ``order`` carries no ``client_order_id``, since nothing can be
        recorded idempotently without one.
    """
    client_order_id = order.client_order_id
    if not client_order_id:
        return []

    base: dict[str, Any] = {
        "client_order_id": client_order_id,
        "bot_id": bot_id,
        "symbol": order.symbol,
        "side": order.side.value,
        "order_type": order.order_type.value,
        "qty": order.qty,
        "price": order.price,
        "ts": ts,
    }
    status = str(result.status).strip().lower()

    if status == "dry_run":
        return [{**base, "kind": "dry_run"}]
    if not result.ok:
        return [{**base, "kind": "rejected", "reason": result.error or status}]

    events: list[dict[str, Any]] = [
        {**base, "kind": "submitted", "venue_order_id": result.order_id}
    ]

    filled_qty = _coerce_float(result.filled_qty)
    if filled_qty is not None and filled_qty > _QTY_EPSILON:
        # `average` is the venue's own volume-weighted fill price. It is absent
        # on some exchanges, where a limit order's price is a far better
        # estimate than zero -- zero would make the fill look free and corrupt
        # cost basis downstream.
        avg_price = _coerce_float(result.raw.get("average")) if result.raw else None
        if avg_price is None or avg_price <= 0:
            avg_price = order.price
        events.append({
            "kind": "order_status",
            "client_order_id": client_order_id,
            "filled_qty": filled_qty,
            "avg_price": avg_price,
            "ts": ts,
        })

    if status in _CANCELED_STATUSES:
        events.append({"kind": "canceled", "client_order_id": client_order_id, "ts": ts})

    return events
