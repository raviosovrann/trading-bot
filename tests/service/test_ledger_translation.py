"""Translating a venue ``OrderResult`` into ledger lifecycle events (#135).

This is the seam where the supervisor stops treating a submission response as
proof that something traded. Every branch here decides whether fill evidence
exists, so it is the last place a submitted-but-unfilled order could still be
mislabelled a trade.
"""

from __future__ import annotations

import pytest

from tradingbot.models import Order, OrderResult, OrderType, Side
from tradingbot.service.ledger import OrderLedger, OrderState, events_from_result


def _order(qty: float = 2.0, *, coid: str = "c1", side: Side = Side.buy) -> Order:
    return Order(
        symbol="BTC/USD",
        side=side,
        order_type=OrderType.market,
        qty=qty,
        client_order_id=coid,
    )


def _result(
    *,
    ok: bool = True,
    status: str = "submitted",
    filled_qty: float = 0.0,
    order_id: str | None = "v1",
    raw: dict | None = None,
    error: str | None = None,
) -> OrderResult:
    return OrderResult(
        ok=ok,
        order_id=order_id,
        status=status,
        filled_qty=filled_qty,
        raw=raw if raw is not None else {},
        error=error,
    )


def _kinds(events: list[dict]) -> list[str]:
    return [event["kind"] for event in events]


def _fold(events: list[dict]) -> OrderLedger:
    ledger = OrderLedger()
    for event in events:
        ledger.apply(event)
    return ledger


class TestDryRun:
    def test_dry_run_produces_only_a_dry_run_event(self) -> None:
        events = events_from_result(
            _order(), _result(status="dry_run", order_id=None), bot_id="bot-a", ts=1
        )
        assert _kinds(events) == ["dry_run"]

    def test_dry_run_never_becomes_a_fill(self) -> None:
        events = events_from_result(
            _order(), _result(status="dry_run", order_id=None), bot_id="bot-a", ts=1
        )
        order = _fold(events).order("c1")
        assert order is not None
        assert order.state is OrderState.dry_run
        assert order.filled_qty == 0.0

    def test_a_dry_run_reporting_a_filled_quantity_is_still_not_a_fill(self) -> None:
        # Nothing reached the venue, so any quantity on the response is noise.
        events = events_from_result(
            _order(),
            _result(status="dry_run", order_id=None, filled_qty=2.0),
            bot_id="bot-a",
            ts=1,
        )
        order = _fold(events).order("c1")
        assert order is not None
        assert order.filled_qty == 0.0


class TestRejection:
    def test_failed_result_is_rejected_not_submitted(self) -> None:
        events = events_from_result(
            _order(),
            _result(ok=False, status="error", order_id=None, error="boom"),
            bot_id="bot-a",
            ts=1,
        )
        assert _kinds(events) == ["rejected"]
        order = _fold(events).order("c1")
        assert order is not None
        assert order.state is OrderState.rejected
        assert order.error == "boom"

    def test_risk_blocked_order_is_rejected(self) -> None:
        events = events_from_result(
            _order(),
            _result(
                ok=False,
                status="risk_blocked",
                order_id=None,
                error="notional cap exceeded",
            ),
            bot_id="bot-a",
            ts=1,
        )
        order = _fold(events).order("c1")
        assert order is not None
        assert order.state is OrderState.rejected
        assert order.error == "notional cap exceeded"

    def test_a_rejected_order_holds_no_quantity(self) -> None:
        events = events_from_result(
            _order(),
            _result(ok=False, status="rejected", filled_qty=2.0, order_id=None),
            bot_id="bot-a",
            ts=1,
        )
        order = _fold(events).order("c1")
        assert order is not None
        assert order.filled_qty == 0.0


class TestAcceptedOrders:
    def test_accepted_without_fill_is_submitted_only(self) -> None:
        events = events_from_result(_order(), _result(), bot_id="bot-a", ts=1)
        assert _kinds(events) == ["submitted"]

        order = _fold(events).order("c1")
        assert order is not None
        assert order.state is OrderState.submitted
        assert order.filled_qty == 0.0
        assert order.venue_order_id == "v1"

    def test_accepted_with_fill_emits_a_status_snapshot(self) -> None:
        events = events_from_result(
            _order(),
            _result(filled_qty=2.0, status="closed", raw={"average": 150.0}),
            bot_id="bot-a",
            ts=1,
        )
        assert _kinds(events) == ["submitted", "order_status"]

        order = _fold(events).order("c1")
        assert order is not None
        assert order.state is OrderState.filled
        assert order.filled_qty == 2.0
        assert order.avg_price == pytest.approx(150.0)

    def test_partial_fill_leaves_the_order_open(self) -> None:
        events = events_from_result(
            _order(qty=2.0),
            _result(filled_qty=0.5, raw={"average": 100.0}),
            bot_id="bot-a",
            ts=1,
        )
        order = _fold(events).order("c1")
        assert order is not None
        assert order.state is OrderState.partially_filled
        assert order.remaining_qty == 1.5

    def test_cancelled_status_is_terminal(self) -> None:
        events = events_from_result(
            _order(), _result(status="canceled"), bot_id="bot-a", ts=1
        )
        assert _kinds(events) == ["submitted", "canceled"]

        order = _fold(events).order("c1")
        assert order is not None
        assert order.state is OrderState.canceled

    def test_cancelled_after_a_partial_fill_keeps_the_filled_quantity(self) -> None:
        events = events_from_result(
            _order(qty=2.0),
            _result(status="canceled", filled_qty=0.5, raw={"average": 100.0}),
            bot_id="bot-a",
            ts=1,
        )
        order = _fold(events).order("c1")
        assert order is not None
        assert order.state is OrderState.canceled
        assert order.filled_qty == 0.5


class TestPriceFallback:
    def test_average_falls_back_to_the_limit_price(self) -> None:
        # ccxt omits `average` on some venues. A limit order's own price is a
        # better estimate than zero, which would make the fill look free.
        order = Order(
            symbol="BTC/USD",
            side=Side.buy,
            order_type=OrderType.limit,
            qty=1.0,
            price=120.0,
            client_order_id="c1",
        )
        events = events_from_result(
            order, _result(filled_qty=1.0, status="closed"), bot_id="bot-a", ts=1
        )
        folded = _fold(events).order("c1")
        assert folded is not None
        assert folded.avg_price == pytest.approx(120.0)

    def test_average_prefers_the_venue_report_over_the_limit_price(self) -> None:
        order = Order(
            symbol="BTC/USD",
            side=Side.buy,
            order_type=OrderType.limit,
            qty=1.0,
            price=120.0,
            client_order_id="c1",
        )
        events = events_from_result(
            order,
            _result(filled_qty=1.0, status="closed", raw={"average": 118.5}),
            bot_id="bot-a",
            ts=1,
        )
        folded = _fold(events).order("c1")
        assert folded is not None
        assert folded.avg_price == pytest.approx(118.5)


class TestEventShape:
    def test_events_carry_the_bot_and_order_identity(self) -> None:
        events = events_from_result(_order(), _result(), bot_id="bot-a", ts=99)
        (submitted,) = events
        assert submitted["client_order_id"] == "c1"
        assert submitted["bot_id"] == "bot-a"
        assert submitted["symbol"] == "BTC/USD"
        assert submitted["side"] == "buy"
        assert submitted["qty"] == 2.0
        assert submitted["ts"] == 99

    def test_translation_is_deterministic(self) -> None:
        # Reconciliation replays translated events; a nondeterministic
        # translation would defeat deduplication.
        order, result = _order(), _result(filled_qty=1.0, raw={"average": 100.0})
        first = events_from_result(order, result, bot_id="bot-a", ts=1)
        second = events_from_result(order, result, bot_id="bot-a", ts=1)
        assert first == second

    def test_replaying_a_translation_does_not_double_count(self) -> None:
        events = events_from_result(
            _order(qty=2.0),
            _result(filled_qty=1.0, raw={"average": 100.0}),
            bot_id="bot-a",
            ts=1,
        )
        order = _fold(events * 3).order("c1")
        assert order is not None
        assert order.filled_qty == 1.0
