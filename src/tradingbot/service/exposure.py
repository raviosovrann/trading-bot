"""Per-bot and global notional exposure, attributed per order.

The accounting this replaces compared each order against the cap on its own
and added the requested notional to a single global float. Three consequences,
all reproducible on the code before #110:

- **Repeated orders never accumulated per bot.** Two 60-notional orders both
  passed a 100 per-bot cap, because neither was measured against the other.
- **Dry runs consumed live exposure.** A `dry_run` result is `ok`, so 120 of
  budget was spent by two orders that never reached a venue.
- **Closing released nothing.** `close_position()` bypassed the accounting
  entirely, so a bot's budget stayed spent after it went flat.

The fix is to attribute exposure **per order**, keyed by client order id,
rather than accumulating a single number. That is what makes it revisable: an
order's true cost is not known when it is submitted -- it depends on whether
it fills, partially fills, is rejected, or is cancelled -- so the submission
figure is a *reservation* that is later replaced by what actually happened.

Keying on the client order id also makes settlement idempotent, which matters
because #135's venue events replay: settling the same order five times must
leave the same exposure as settling it once.
"""

from __future__ import annotations

import math
import threading


class ExposureTracker:
    """Tracks reserved and confirmed notional per bot and in total.

    Thread-safe. Order placement runs on per-bot worker threads (#111), not on
    the event loop, so concurrent bots genuinely race here -- the cap check and
    the reservation must be one atomic step or two bots can both pass the same
    remaining budget.
    """

    def __init__(self) -> None:
        """Initialize the tracker.

        Holds no caps of its own. Limits are policy and live with the caller
        -- each bot's configuration carries both its own cap and the global
        one -- while this only tracks what is currently at risk.
        """
        self._lock = threading.Lock()
        self._orders: dict[str, dict[str, float]] = {}
        """bot id -> client order id -> notional currently attributed."""

    def reserve(
        self,
        bot_id: str,
        client_order_id: str,
        notional: float,
        *,
        per_bot_cap: float,
        global_cap: float,
    ) -> bool:
        """Attempt to reserve ``notional`` for an order about to be submitted.

        Checks the resulting *cumulative* exposure -- this bot's and the
        global total -- rather than the order in isolation. Re-reserving an id
        that is already held replaces its figure instead of adding to it, so a
        retried submission of the same order is not counted twice.

        Args:
            bot_id: Bot submitting the order.
            client_order_id: The order's idempotency key.
            notional: Quote-currency exposure the order would add.
            per_bot_cap: This bot's cap.
            global_cap: Cap across all bots.

        Returns:
            ``True`` if the reservation was made. ``False`` if it would breach
            either cap, or if ``notional`` is not a usable number -- in which
            case nothing is charged.
        """
        if not math.isfinite(notional) or notional < 0:
            return False

        with self._lock:
            bot_orders = self._orders.setdefault(bot_id, {})
            held = bot_orders.get(client_order_id, 0.0)
            # Exclude this order's own previous figure from both totals, so a
            # replacement is measured as a replacement.
            bot_used = sum(bot_orders.values()) - held
            global_used = self._total_locked() - held

            if bot_used + notional > per_bot_cap:
                return False
            if global_used + notional > global_cap:
                return False

            bot_orders[client_order_id] = notional
            return True

    def settle(self, bot_id: str, client_order_id: str, notional: float) -> None:
        """Replace an order's attributed exposure with what actually happened.

        Called once the order's fate is known: zero for a dry run, a
        rejection, or a cancel with no fill; the filled notional for a fill.
        Unlike ``reserve`` this never refuses. A fill is a fact rather than a
        request, so a figure above the cap is *recorded* -- refusing to record
        real exposure would understate risk, which is the opposite of the
        point of a cap.

        Settling an order that was never reserved is allowed: after a restart
        the ledger is replayed into a process that reserved nothing, and those
        positions are real.

        Args:
            bot_id: Bot the order belongs to.
            client_order_id: The order's idempotency key.
            notional: Exposure now attributable to it; ``0`` releases it.
        """
        if not math.isfinite(notional):
            return
        amount = max(0.0, notional)

        with self._lock:
            bot_orders = self._orders.setdefault(bot_id, {})
            if amount == 0.0:
                bot_orders.pop(client_order_id, None)
            else:
                bot_orders[client_order_id] = amount

    def reduce_bot(self, bot_id: str, notional: float) -> None:
        """Reduce ``bot_id``'s exposure by ``notional``.

        For a reduce-only fill, which shrinks a position rather than opening
        one. Applied across the bot's attributed orders oldest first, since a
        reduction is against the position as a whole and cannot be tied to the
        order that originally opened it.

        Floors at zero. A venue reporting more reduced than we believed was
        held must not produce negative exposure, which would hand out budget
        the bot never earned.

        Args:
            bot_id: Bot whose exposure to reduce.
            notional: Quote-currency amount to release.
        """
        if not math.isfinite(notional) or notional <= 0:
            return

        with self._lock:
            bot_orders = self._orders.get(bot_id)
            if not bot_orders:
                return
            remaining = notional
            for key in list(bot_orders):
                if remaining <= 0:
                    break
                held = bot_orders[key]
                applied = min(held, remaining)
                remaining -= applied
                if held - applied <= 0:
                    del bot_orders[key]
                else:
                    bot_orders[key] = held - applied

    def release_bot(self, bot_id: str) -> None:
        """Drop all exposure attributed to ``bot_id``.

        For a bot that has gone flat or been removed. Deliberately blunt: it
        is only correct where the caller knows the bot holds nothing.

        Args:
            bot_id: Bot to clear.
        """
        with self._lock:
            self._orders.pop(bot_id, None)

    def used(self, bot_id: str) -> float:
        """Return the notional currently attributed to ``bot_id``."""
        with self._lock:
            return sum(self._orders.get(bot_id, {}).values())

    def total(self) -> float:
        """Return the notional attributed across all bots."""
        with self._lock:
            return self._total_locked()

    def _total_locked(self) -> float:
        """Return the global total. Caller must hold ``self._lock``."""
        return sum(
            notional
            for bot_orders in self._orders.values()
            for notional in bot_orders.values()
        )
