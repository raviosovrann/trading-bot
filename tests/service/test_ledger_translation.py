"""Translating a venue ``OrderResult`` into ledger lifecycle events (#135).

This is the seam where the supervisor stops treating a submission response as
proof that something traded. Every branch here decides whether fill evidence
exists, so it is the last place a submitted-but-unfilled order could still be
mislabelled a trade.
"""

from __future__ import annotations

import pytest

from tradingbot.models import Order, OrderResult, OrderType, Side
from tradingbot.service.ledger import (
    OrderLedger,
    OrderState,
    events_from_payload,
    events_from_status,
    events_from_result,
)


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


class TestPayloadTranslation:
    """The supervisor receives runtime events as JSON, not as model objects."""

    def _payload(self, *, order: dict | None, result: dict, ts: int = 7) -> dict:
        return {"type": "order", "ts": ts, "order": order, "result": result}

    def test_payload_round_trips_into_lifecycle_events(self) -> None:
        payload = self._payload(
            order={
                "symbol": "BTC/USD",
                "side": "buy",
                "order_type": "market",
                "qty": 2.0,
                "price": None,
                "reduce_only": False,
                "client_order_id": "c1",
            },
            result={
                "ok": True,
                "order_id": "v1",
                "status": "closed",
                "filled_qty": 2.0,
                "raw": {"average": 150.0},
                "error": None,
            },
        )

        events = events_from_payload(payload, bot_id="bot-a")
        order = _fold(events).order("c1")
        assert order is not None
        assert order.state is OrderState.filled
        assert order.filled_qty == 2.0
        assert order.avg_price == pytest.approx(150.0)
        assert order.bot_id == "bot-a"

    def test_a_close_carries_no_order_and_yields_no_events(self) -> None:
        # close_position() constructs no Order, so there is no idempotency key
        # to record against. Position effects are picked up by the refresh.
        payload = self._payload(
            order=None,
            result={
                "ok": True,
                "order_id": "v9",
                "status": "closed",
                "filled_qty": 1.0,
                "raw": {},
                "error": None,
            },
        )

        assert events_from_payload(payload, bot_id="bot-a") == []

    def test_a_legacy_payload_without_order_or_result_yields_no_events(self) -> None:
        # Events emitted before this change carry only the flat fields. They
        # must not crash the supervisor or invent fill evidence.
        assert events_from_payload({"type": "order", "status": "filled"}, bot_id="b") == []

    def test_a_malformed_payload_yields_no_events(self) -> None:
        payload = self._payload(order={"nonsense": True}, result={"also": "nonsense"})
        assert events_from_payload(payload, bot_id="bot-a") == []

    def test_payload_timestamp_is_carried_onto_the_events(self) -> None:
        payload = self._payload(
            order={
                "symbol": "BTC/USD",
                "side": "buy",
                "order_type": "market",
                "qty": 1.0,
                "price": None,
                "reduce_only": False,
                "client_order_id": "c1",
            },
            result={
                "ok": True, "order_id": "v1", "status": "open",
                "filled_qty": 0.0, "raw": {}, "error": None,
            },
            ts=1_700_000_000,
        )

        (submitted,) = events_from_payload(payload, bot_id="bot-a")
        assert submitted["ts"] == 1_700_000_000


class TestStatusPollTranslation:
    """Translating an order-status poll response (#135, commit 3).

    The poller re-reads orders the venue may know more about than we do. Its
    responses are cumulative, so they become `order_status` events -- never
    `fill` events, which would double-count on the second read of an order.
    """

    def test_open_order_with_no_fill_yields_nothing(self) -> None:
        # Nothing has changed, so there is nothing to record.
        assert events_from_status("c1", _result(status="open"), ts=5) == []

    def test_open_order_with_a_partial_fill_yields_a_snapshot(self) -> None:
        events = events_from_status(
            "c1", _result(status="open", filled_qty=0.5, raw={"average": 100.0}), ts=5
        )
        assert _kinds(events) == ["order_status"]
        assert events[0]["filled_qty"] == 0.5
        assert events[0]["avg_price"] == 100.0

    def test_closed_order_yields_a_snapshot(self) -> None:
        events = events_from_status(
            "c1", _result(status="closed", filled_qty=2.0, raw={"average": 150.0}), ts=5
        )
        assert _kinds(events) == ["order_status"]

    def test_canceled_order_yields_its_fill_then_the_cancel(self) -> None:
        # Order matters: the quantity that traded before the cancel has to be
        # recorded, and an explicit terminal event is refused once set.
        events = events_from_status(
            "c1",
            _result(status="canceled", filled_qty=0.5, raw={"average": 100.0}),
            ts=5,
        )
        assert _kinds(events) == ["order_status", "canceled"]

    def test_canceled_order_with_no_fill_yields_only_the_cancel(self) -> None:
        events = events_from_status("c1", _result(status="canceled"), ts=5)
        assert _kinds(events) == ["canceled"]

    def test_rejected_order_yields_a_rejection(self) -> None:
        events = events_from_status(
            "c1", _result(status="rejected", error="margin"), ts=5
        )
        assert _kinds(events) == ["rejected"]
        assert events[0]["reason"] == "margin"

    def test_a_failed_poll_is_not_evidence_of_anything(self) -> None:
        # A network error means we do not know the order's state. Recording it
        # as a rejection would cancel a live order in our books while it keeps
        # working at the venue -- the worst outcome available here.
        assert events_from_status(
            "c1", _result(ok=False, status="error", error="timeout"), ts=5
        ) == []

    def test_polling_twice_does_not_double_count(self) -> None:
        response = _result(status="closed", filled_qty=2.0, raw={"average": 150.0})
        ledger = OrderLedger()
        ledger.apply(_submitted_event())
        for _ in range(3):
            for event in events_from_status("c1", response, ts=5):
                ledger.apply(event)

        order = ledger.order("c1")
        assert order is not None
        assert order.filled_qty == 2.0

    def test_a_poll_never_emits_a_discrete_fill(self) -> None:
        # Guards the commit-1b invariant directly: a cumulative source must
        # not enter the ledger through the incremental path.
        for status in ("open", "closed", "canceled", "rejected"):
            events = events_from_status(
                "c1", _result(status=status, filled_qty=1.0), ts=5
            )
            assert "fill" not in _kinds(events)


def _submitted_event() -> dict:
    return {
        "kind": "submitted",
        "client_order_id": "c1",
        "bot_id": "bot-a",
        "symbol": "BTC/USD",
        "side": "buy",
        "order_type": "market",
        "qty": 2.0,
        "price": None,
        "venue_order_id": "v1",
        "ts": 1,
    }
