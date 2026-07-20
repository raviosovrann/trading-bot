"""Order/execution ledger projection (#135).

The ledger folds an append-only lifecycle event log into current order state
and a fill history. It is the single source of truth that exposure (#110) and
spot cost basis (#128) are computed from, so the properties asserted here --
idempotency, out-of-order tolerance, and never inventing fill evidence -- are
load-bearing for those issues rather than nice-to-have.
"""

from __future__ import annotations

import pytest

from tradingbot.service.ledger import (
    Execution,
    OrderLedger,
    OrderRecord,
    OrderState,
)


def _submitted(
    coid: str = "c1",
    *,
    bot_id: str = "bot-a",
    symbol: str = "BTC/USD",
    side: str = "buy",
    qty: float = 2.0,
    price: float | None = None,
    venue_order_id: str | None = "v1",
    ts: int = 1_000,
) -> dict:
    return {
        "kind": "submitted",
        "client_order_id": coid,
        "bot_id": bot_id,
        "symbol": symbol,
        "side": side,
        "order_type": "market",
        "qty": qty,
        "price": price,
        "venue_order_id": venue_order_id,
        "ts": ts,
    }


def _fill(
    exec_id: str,
    *,
    coid: str = "c1",
    qty: float = 1.0,
    price: float = 100.0,
    fee: float = 0.0,
    ts: int = 1_100,
) -> dict:
    return {
        "kind": "fill",
        "exec_id": exec_id,
        "client_order_id": coid,
        "qty": qty,
        "price": price,
        "fee": fee,
        "fee_currency": "USD",
        "ts": ts,
    }


def _status(
    coid: str = "c1",
    *,
    filled_qty: float,
    avg_price: float,
    fees: float | None = None,
    ts: int = 1_100,
) -> dict:
    """A cumulative order-status snapshot, as ccxt and order-status polls report."""
    event: dict = {
        "kind": "order_status",
        "client_order_id": coid,
        "filled_qty": filled_qty,
        "avg_price": avg_price,
        "ts": ts,
    }
    if fees is not None:
        event["fees"] = fees
    return event


class TestSubmissionIsNotAFill:
    """The headline acceptance criterion: no fill evidence, no filled trade."""

    def test_submitted_order_has_no_filled_quantity(self) -> None:
        ledger = OrderLedger()
        ledger.apply(_submitted())

        order = ledger.order("c1")
        assert order is not None
        assert order.state is OrderState.submitted
        assert order.filled_qty == 0.0
        assert order.avg_price == 0.0

    def test_submitted_order_produces_no_executions(self) -> None:
        ledger = OrderLedger()
        ledger.apply(_submitted())

        assert ledger.executions() == []

    def test_dry_run_is_terminal_and_never_a_fill(self) -> None:
        ledger = OrderLedger()
        ledger.apply({**_submitted(), "kind": "dry_run", "venue_order_id": None})

        order = ledger.order("c1")
        assert order is not None
        assert order.state is OrderState.dry_run
        assert order.is_terminal
        assert order.filled_qty == 0.0
        assert ledger.executions() == []


class TestFillProgression:
    def test_partial_fill_moves_to_partially_filled(self) -> None:
        ledger = OrderLedger()
        ledger.apply(_submitted(qty=2.0))
        ledger.apply(_fill("e1", qty=0.5, price=100.0))

        order = ledger.order("c1")
        assert order is not None
        assert order.state is OrderState.partially_filled
        assert order.filled_qty == 0.5
        assert order.remaining_qty == 1.5
        assert not order.is_terminal

    def test_completing_the_quantity_moves_to_filled(self) -> None:
        ledger = OrderLedger()
        ledger.apply(_submitted(qty=2.0))
        ledger.apply(_fill("e1", qty=0.5, price=100.0))
        ledger.apply(_fill("e2", qty=1.5, price=200.0))

        order = ledger.order("c1")
        assert order is not None
        assert order.state is OrderState.filled
        assert order.filled_qty == 2.0
        assert order.is_terminal

    def test_average_price_is_quantity_weighted(self) -> None:
        ledger = OrderLedger()
        ledger.apply(_submitted(qty=2.0))
        ledger.apply(_fill("e1", qty=0.5, price=100.0))
        ledger.apply(_fill("e2", qty=1.5, price=200.0))

        order = ledger.order("c1")
        assert order is not None
        # (0.5*100 + 1.5*200) / 2.0 == 175, not the 150 a naive mean would give.
        assert order.avg_price == pytest.approx(175.0)

    def test_fees_accumulate_across_fills(self) -> None:
        ledger = OrderLedger()
        ledger.apply(_submitted(qty=2.0))
        ledger.apply(_fill("e1", qty=1.0, fee=0.10))
        ledger.apply(_fill("e2", qty=1.0, fee=0.25))

        order = ledger.order("c1")
        assert order is not None
        assert order.fees == pytest.approx(0.35)


class TestIdempotency:
    """Venues replay. A replayed event must not double-count."""

    def test_duplicate_fill_is_ignored(self) -> None:
        ledger = OrderLedger()
        ledger.apply(_submitted(qty=2.0))
        assert ledger.apply(_fill("e1", qty=1.0)) is True
        assert ledger.apply(_fill("e1", qty=1.0)) is False

        order = ledger.order("c1")
        assert order is not None
        assert order.filled_qty == 1.0
        assert len(ledger.executions()) == 1

    def test_duplicate_submission_does_not_reset_progress(self) -> None:
        ledger = OrderLedger()
        ledger.apply(_submitted(qty=2.0))
        ledger.apply(_fill("e1", qty=1.0))
        ledger.apply(_submitted(qty=2.0))

        order = ledger.order("c1")
        assert order is not None
        assert order.filled_qty == 1.0
        assert order.state is OrderState.partially_filled

    def test_duplicate_submission_reports_no_change(self) -> None:
        # apply() returning False is how a caller persisting the log decides
        # not to write an event that carries no information. A re-delivered
        # submission on reconnect is the common case.
        ledger = OrderLedger()
        assert ledger.apply(_submitted(qty=2.0)) is True
        assert ledger.apply(_submitted(qty=2.0)) is False

    def test_duplicate_terminal_reports_no_change(self) -> None:
        ledger = OrderLedger()
        ledger.apply(_submitted(qty=2.0))
        cancel = {"kind": "canceled", "client_order_id": "c1", "ts": 1_200}
        assert ledger.apply(cancel) is True
        assert ledger.apply(cancel) is False

    def test_first_terminal_event_wins_over_a_contradicting_one(self) -> None:
        # A venue reporting both a cancel and a rejection is contradicting
        # itself. Keeping the first is arbitrary but stable, and replay needs
        # stability more than it needs the "right" answer here.
        ledger = OrderLedger()
        ledger.apply(_submitted(qty=2.0))
        ledger.apply({"kind": "canceled", "client_order_id": "c1", "ts": 1_200})
        ledger.apply({"kind": "rejected", "client_order_id": "c1", "ts": 1_300})

        order = ledger.order("c1")
        assert order is not None
        assert order.state is OrderState.canceled

    def test_replaying_the_whole_log_is_a_no_op(self) -> None:
        events = [
            _submitted(qty=2.0),
            _fill("e1", qty=1.0, fee=0.1),
            _fill("e2", qty=1.0, fee=0.1),
        ]
        first = OrderLedger()
        for event in events:
            first.apply(event)

        replayed = OrderLedger()
        for event in events * 2:
            replayed.apply(event)

        assert replayed.order("c1") == first.order("c1")
        assert replayed.executions() == first.executions()


class TestOutOfOrderEvents:
    """Reconnect and restart deliver events in whatever order they like."""

    def test_fill_arriving_before_its_submission_is_retained(self) -> None:
        ledger = OrderLedger()
        ledger.apply(_fill("e1", qty=1.0, price=100.0))
        ledger.apply(_submitted(qty=2.0))

        order = ledger.order("c1")
        assert order is not None
        assert order.filled_qty == 1.0
        assert order.state is OrderState.partially_filled

    def test_terminal_state_is_not_reopened_by_a_late_submission(self) -> None:
        ledger = OrderLedger()
        ledger.apply(_submitted(qty=2.0))
        ledger.apply({"kind": "canceled", "client_order_id": "c1", "ts": 1_200})
        ledger.apply(_submitted(qty=2.0, ts=1_300))

        order = ledger.order("c1")
        assert order is not None
        assert order.state is OrderState.canceled

    def test_a_fill_after_cancel_still_counts(self) -> None:
        # The exchange filled part of it before the cancel landed. The position
        # is real regardless of the order the two events reach us in.
        ledger = OrderLedger()
        ledger.apply(_submitted(qty=2.0))
        ledger.apply({"kind": "canceled", "client_order_id": "c1", "ts": 1_200})
        ledger.apply(_fill("e1", qty=0.5, price=100.0, ts=1_150))

        order = ledger.order("c1")
        assert order is not None
        assert order.filled_qty == 0.5
        assert order.state is OrderState.canceled


class TestPartialFillThenCancel:
    def test_position_reflects_only_the_filled_part(self) -> None:
        ledger = OrderLedger()
        ledger.apply(_submitted(qty=2.0, side="buy"))
        ledger.apply(_fill("e1", qty=0.75, price=100.0, fee=0.05))
        ledger.apply({"kind": "canceled", "client_order_id": "c1", "ts": 1_200})

        order = ledger.order("c1")
        assert order is not None
        assert order.state is OrderState.canceled
        assert order.filled_qty == 0.75
        assert order.remaining_qty == 1.25
        assert order.is_terminal


class TestRejection:
    def test_rejected_order_is_terminal_with_no_fill(self) -> None:
        ledger = OrderLedger()
        ledger.apply(_submitted(qty=2.0))
        ledger.apply({
            "kind": "rejected",
            "client_order_id": "c1",
            "reason": "insufficient funds",
            "ts": 1_200,
        })

        order = ledger.order("c1")
        assert order is not None
        assert order.state is OrderState.rejected
        assert order.is_terminal
        assert order.filled_qty == 0.0
        assert order.error == "insufficient funds"


class TestOpenOrders:
    """Reconciliation after restart needs to know what is still live."""

    def test_open_orders_exclude_terminal_ones(self) -> None:
        ledger = OrderLedger()
        ledger.apply(_submitted("c1", qty=2.0))
        ledger.apply(_submitted("c2", qty=1.0, venue_order_id="v2"))
        ledger.apply(_fill("e1", coid="c2", qty=1.0))
        ledger.apply(_submitted("c3", qty=1.0, venue_order_id="v3"))
        ledger.apply({"kind": "rejected", "client_order_id": "c3", "ts": 1_200})

        assert [o.client_order_id for o in ledger.open_orders()] == ["c1"]

    def test_partially_filled_order_is_still_open(self) -> None:
        ledger = OrderLedger()
        ledger.apply(_submitted(qty=2.0))
        ledger.apply(_fill("e1", qty=1.0))

        assert [o.client_order_id for o in ledger.open_orders()] == ["c1"]


class TestScoping:
    def test_orders_are_scoped_per_bot(self) -> None:
        ledger = OrderLedger()
        ledger.apply(_submitted("c1", bot_id="bot-a"))
        ledger.apply(_submitted("c2", bot_id="bot-b", venue_order_id="v2"))

        assert [o.client_order_id for o in ledger.orders(bot_id="bot-a")] == ["c1"]
        assert [o.client_order_id for o in ledger.orders(bot_id="bot-b")] == ["c2"]


class TestMalformedEvents:
    """A corrupt record must not take the projection down -- #108's lesson."""

    def test_unknown_kind_is_ignored(self) -> None:
        ledger = OrderLedger()
        assert ledger.apply({"kind": "nonsense", "client_order_id": "c1"}) is False
        assert ledger.orders() == []

    def test_fill_without_a_client_order_id_is_ignored(self) -> None:
        ledger = OrderLedger()
        assert ledger.apply({"kind": "fill", "exec_id": "e1", "qty": 1.0}) is False
        assert ledger.executions() == []

    def test_non_positive_fill_quantity_is_ignored(self) -> None:
        ledger = OrderLedger()
        ledger.apply(_submitted(qty=2.0))
        assert ledger.apply(_fill("e1", qty=0.0)) is False
        assert ledger.apply(_fill("e2", qty=-1.0)) is False

        order = ledger.order("c1")
        assert order is not None
        assert order.filled_qty == 0.0


class TestCumulativeStatusSnapshots:
    """Most venues report cumulative filled quantity, not per-fill deltas.

    ccxt returns ``filled``/``average`` for the whole order on both the
    submission response and ``fetch_order``. Applying those as increments would
    double-count the moment the commit-3 poller re-reads an order it has
    already seen, so they are applied as a monotonic set instead.
    """

    def test_snapshot_sets_filled_quantity(self) -> None:
        ledger = OrderLedger()
        ledger.apply(_submitted(qty=2.0))
        ledger.apply(_status("c1", filled_qty=1.0, avg_price=100.0))

        order = ledger.order("c1")
        assert order is not None
        assert order.filled_qty == 1.0
        assert order.avg_price == pytest.approx(100.0)
        assert order.state is OrderState.partially_filled

    def test_repeated_identical_snapshot_does_not_accumulate(self) -> None:
        ledger = OrderLedger()
        ledger.apply(_submitted(qty=2.0))
        assert ledger.apply(_status("c1", filled_qty=1.0, avg_price=100.0)) is True
        assert ledger.apply(_status("c1", filled_qty=1.0, avg_price=100.0)) is False

        order = ledger.order("c1")
        assert order is not None
        assert order.filled_qty == 1.0

    def test_snapshot_advances_monotonically(self) -> None:
        ledger = OrderLedger()
        ledger.apply(_submitted(qty=2.0))
        ledger.apply(_status("c1", filled_qty=1.0, avg_price=100.0))
        ledger.apply(_status("c1", filled_qty=2.0, avg_price=150.0))

        order = ledger.order("c1")
        assert order is not None
        assert order.filled_qty == 2.0
        assert order.state is OrderState.filled

    def test_stale_snapshot_never_walks_progress_backward(self) -> None:
        # An out-of-order poll response is older than what we already know.
        # Believing it would resurrect a filled order and re-reserve exposure.
        ledger = OrderLedger()
        ledger.apply(_submitted(qty=2.0))
        ledger.apply(_status("c1", filled_qty=2.0, avg_price=150.0))
        assert ledger.apply(_status("c1", filled_qty=0.0, avg_price=0.0)) is False

        order = ledger.order("c1")
        assert order is not None
        assert order.filled_qty == 2.0
        assert order.state is OrderState.filled


class TestSnapshotAndFillsTogether:
    """A venue may push discrete executions AND answer status polls.

    Summing both sources double-counts. Taking the greater of the two believes
    whichever source is further ahead, which is the safe direction: it never
    understates a position, and never releases exposure that is still at risk.
    """

    def test_snapshot_and_fills_are_not_summed(self) -> None:
        ledger = OrderLedger()
        ledger.apply(_submitted(qty=2.0))
        ledger.apply(_fill("e1", qty=1.0, price=100.0))
        ledger.apply(_status("c1", filled_qty=1.0, avg_price=100.0))

        order = ledger.order("c1")
        assert order is not None
        assert order.filled_qty == 1.0

    def test_fills_ahead_of_the_snapshot_win(self) -> None:
        ledger = OrderLedger()
        ledger.apply(_submitted(qty=2.0))
        ledger.apply(_fill("e1", qty=1.0, price=100.0))
        ledger.apply(_fill("e2", qty=0.5, price=100.0))
        ledger.apply(_status("c1", filled_qty=1.0, avg_price=100.0))

        order = ledger.order("c1")
        assert order is not None
        assert order.filled_qty == 1.5

    def test_snapshot_ahead_of_the_fills_wins(self) -> None:
        # Fills for the rest of the quantity have not reached us yet.
        ledger = OrderLedger()
        ledger.apply(_submitted(qty=2.0))
        ledger.apply(_fill("e1", qty=0.5, price=100.0))
        ledger.apply(_status("c1", filled_qty=2.0, avg_price=120.0))

        order = ledger.order("c1")
        assert order is not None
        assert order.filled_qty == 2.0
        assert order.state is OrderState.filled

    def test_fill_derived_average_is_preferred_when_fills_lead(self) -> None:
        # Fills carry exact per-execution prices; a snapshot average is
        # rounded by the venue. Trust the finer-grained source when it is the
        # one that is ahead.
        ledger = OrderLedger()
        ledger.apply(_submitted(qty=2.0))
        ledger.apply(_fill("e1", qty=1.0, price=100.0))
        ledger.apply(_fill("e2", qty=1.0, price=200.0))
        ledger.apply(_status("c1", filled_qty=1.0, avg_price=999.0))

        order = ledger.order("c1")
        assert order is not None
        assert order.avg_price == pytest.approx(150.0)


class TestRecordTypes:
    def test_execution_carries_venue_identity(self) -> None:
        ledger = OrderLedger()
        ledger.apply(_submitted(qty=1.0))
        ledger.apply(_fill("e1", qty=1.0, price=100.0, fee=0.2))

        (execution,) = ledger.executions()
        assert isinstance(execution, Execution)
        assert execution.exec_id == "e1"
        assert execution.client_order_id == "c1"
        assert execution.bot_id == "bot-a"
        assert execution.symbol == "BTC/USD"
        assert execution.side == "buy"
        assert execution.qty == 1.0
        assert execution.price == 100.0
        assert execution.fee == pytest.approx(0.2)

    def test_order_record_is_immutable(self) -> None:
        ledger = OrderLedger()
        ledger.apply(_submitted())
        order = ledger.order("c1")
        assert isinstance(order, OrderRecord)

        with pytest.raises((AttributeError, TypeError)):
            order.filled_qty = 99.0  # type: ignore[misc]
