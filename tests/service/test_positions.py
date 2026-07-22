"""Per-bot spot position and cost basis (#128).

`CcxtVenue.get_position()` returns the whole account's base-asset balance as
this bot's position, with `entry_price=0.0`, and `close_position()` sells all
of it. So a bot could liquidate coins a person bought by hand, or another
bot's position on the same account, and its reported "PnL" was really the
holding's market value.

The fix is to derive what a bot owns from its own fills. These tests pin the
consequence: a bot's position is what it bought, never what the account holds.
"""

from __future__ import annotations

import pytest

from tradingbot.service.ledger import OrderLedger
from tradingbot.service.positions import SpotPosition, spot_position


def _buy(coid: str, qty: float, price: float, *, ts: int = 1, fee: float = 0.0) -> list[dict]:
    return _order(coid, "buy", qty, price, ts=ts, fee=fee)


def _sell(coid: str, qty: float, price: float, *, ts: int = 1, fee: float = 0.0) -> list[dict]:
    return _order(coid, "sell", qty, price, ts=ts, fee=fee)


def _order(coid, side, qty, price, *, ts, fee) -> list[dict]:
    return [
        {
            "kind": "submitted", "client_order_id": coid, "bot_id": "bot-a",
            "symbol": "BTC/USD", "side": side, "order_type": "market",
            "qty": qty, "price": None, "venue_order_id": f"v-{coid}", "ts": ts,
        },
        {
            "kind": "order_status", "client_order_id": coid, "filled_qty": qty,
            "avg_price": price, "fees": fee, "ts": ts + 1,
        },
    ]


def _position(*event_groups) -> SpotPosition:
    ledger = OrderLedger()
    for group in event_groups:
        for event in group:
            ledger.apply(event)
    return spot_position(ledger.orders(bot_id="bot-a"))


class TestOwnership:
    """The headline: a bot owns what it bought, not what the account holds."""

    def test_a_bot_that_never_traded_owns_nothing(self) -> None:
        assert _position().quantity == 0.0

    def test_a_buy_is_owned(self) -> None:
        assert _position(_buy("c1", 2.0, 100.0)).quantity == pytest.approx(2.0)

    def test_a_sell_reduces_what_is_owned(self) -> None:
        position = _position(_buy("c1", 2.0, 100.0), _sell("c2", 0.5, 120.0, ts=10))

        assert position.quantity == pytest.approx(1.5)

    def test_selling_everything_leaves_nothing_owned(self) -> None:
        position = _position(_buy("c1", 2.0, 100.0), _sell("c2", 2.0, 120.0, ts=10))

        assert position.quantity == pytest.approx(0.0)
        assert position.is_flat

    def test_a_dry_run_is_not_owned(self) -> None:
        events = [{
            "kind": "dry_run", "client_order_id": "c1", "bot_id": "bot-a",
            "symbol": "BTC/USD", "side": "buy", "order_type": "market",
            "qty": 2.0, "price": None, "ts": 1,
        }]

        assert _position(events).quantity == 0.0

    def test_a_rejected_order_is_not_owned(self) -> None:
        events = _buy("c1", 2.0, 100.0)[:1] + [
            {"kind": "rejected", "client_order_id": "c1", "reason": "no", "ts": 2}
        ]

        assert _position(events).quantity == 0.0

    def test_a_submitted_but_unfilled_order_is_not_owned(self) -> None:
        # Ownership needs fill evidence, exactly as #135 established.
        assert _position(_buy("c1", 2.0, 100.0)[:1]).quantity == 0.0

    def test_a_partial_fill_is_owned_only_up_to_what_filled(self) -> None:
        events = _buy("c1", 2.0, 100.0)[:1] + [
            {"kind": "order_status", "client_order_id": "c1",
             "filled_qty": 0.5, "avg_price": 100.0, "ts": 2},
        ]

        assert _position(events).quantity == pytest.approx(0.5)


class TestCostBasis:
    def test_average_cost_of_a_single_buy(self) -> None:
        assert _position(_buy("c1", 2.0, 100.0)).average_cost == pytest.approx(100.0)

    def test_average_cost_is_quantity_weighted_across_buys(self) -> None:
        position = _position(
            _buy("c1", 1.0, 100.0), _buy("c2", 3.0, 200.0, ts=10)
        )

        # (1*100 + 3*200) / 4 = 175, not the 150 a naive mean gives.
        assert position.average_cost == pytest.approx(175.0)

    def test_a_sell_does_not_change_the_average_cost(self) -> None:
        # Under average-cost accounting a disposal realises profit; it does
        # not re-price what is left.
        position = _position(
            _buy("c1", 2.0, 100.0), _sell("c2", 1.0, 500.0, ts=10)
        )

        assert position.average_cost == pytest.approx(100.0)

    def test_selling_out_completely_resets_the_basis(self) -> None:
        position = _position(
            _buy("c1", 2.0, 100.0), _sell("c2", 2.0, 120.0, ts=10)
        )

        assert position.average_cost == 0.0

    def test_buying_back_after_a_full_exit_starts_a_fresh_basis(self) -> None:
        position = _position(
            _buy("c1", 1.0, 100.0),
            _sell("c2", 1.0, 120.0, ts=10),
            _buy("c3", 1.0, 300.0, ts=20),
        )

        assert position.average_cost == pytest.approx(300.0)


class TestRealizedPnl:
    def test_no_sales_means_nothing_realized(self) -> None:
        assert _position(_buy("c1", 2.0, 100.0)).realized_pnl == 0.0

    def test_selling_above_cost_realizes_a_profit(self) -> None:
        position = _position(
            _buy("c1", 2.0, 100.0), _sell("c2", 1.0, 150.0, ts=10)
        )

        # 1 sold at 150, cost 100 -> +50.
        assert position.realized_pnl == pytest.approx(50.0)

    def test_selling_below_cost_realizes_a_loss(self) -> None:
        position = _position(
            _buy("c1", 2.0, 100.0), _sell("c2", 1.0, 80.0, ts=10)
        )

        assert position.realized_pnl == pytest.approx(-20.0)

    def test_realized_pnl_accumulates_across_sales(self) -> None:
        position = _position(
            _buy("c1", 3.0, 100.0),
            _sell("c2", 1.0, 150.0, ts=10),
            _sell("c3", 1.0, 200.0, ts=20),
        )

        assert position.realized_pnl == pytest.approx(150.0)

    def test_fees_reduce_realized_pnl(self) -> None:
        position = _position(
            _buy("c1", 2.0, 100.0, fee=1.0),
            _sell("c2", 1.0, 150.0, ts=10, fee=2.0),
        )

        # +50 gross, less 3.0 of fees.
        assert position.realized_pnl == pytest.approx(47.0)


class TestUnrealizedPnl:
    def test_unrealized_marks_the_held_quantity_to_market(self) -> None:
        position = _position(_buy("c1", 2.0, 100.0))

        assert position.unrealized_pnl(150.0) == pytest.approx(100.0)

    def test_unrealized_is_zero_when_flat(self) -> None:
        position = _position(_buy("c1", 1.0, 100.0), _sell("c2", 1.0, 150.0, ts=10))

        assert position.unrealized_pnl(999.0) == 0.0

    def test_unrealized_uses_cost_basis_not_zero(self) -> None:
        """The old code used entry_price=0.0, making PnL the market value."""
        position = _position(_buy("c1", 2.0, 100.0))

        assert position.unrealized_pnl(100.0) == pytest.approx(0.0)
        assert position.unrealized_pnl(100.0) != pytest.approx(200.0)

    def test_an_unusable_mark_price_yields_no_unrealized(self) -> None:
        position = _position(_buy("c1", 2.0, 100.0))

        for price in (0.0, -1.0, float("nan")):
            assert position.unrealized_pnl(price) == 0.0


class TestOverSelling:
    """Selling more than owned must not create a negative spot position."""

    def test_quantity_floors_at_zero(self) -> None:
        position = _position(
            _buy("c1", 1.0, 100.0), _sell("c2", 5.0, 120.0, ts=10)
        )

        assert position.quantity == 0.0

    def test_only_the_owned_part_realizes_pnl(self) -> None:
        # 1 owned at 100, sold at 120 -> +20. The 4 we never had contribute
        # nothing rather than fabricating profit against a zero basis.
        position = _position(
            _buy("c1", 1.0, 100.0), _sell("c2", 5.0, 120.0, ts=10)
        )

        assert position.realized_pnl == pytest.approx(20.0)


class TestOrdering:
    def test_events_are_applied_in_time_order(self) -> None:
        # Fed newest-first; the result must match chronological application.
        position = _position(
            _sell("c2", 1.0, 150.0, ts=10), _buy("c1", 2.0, 100.0, ts=1)
        )

        assert position.quantity == pytest.approx(1.0)
        assert position.realized_pnl == pytest.approx(50.0)


class TestIsolation:
    def test_another_bots_orders_are_not_counted(self) -> None:
        ledger = OrderLedger()
        for event in _buy("c1", 2.0, 100.0):
            ledger.apply(event)
        for event in _buy("c2", 9.0, 100.0):
            ledger.apply({**event, "bot_id": "bot-b"})

        mine = spot_position(ledger.orders(bot_id="bot-a"))

        assert mine.quantity == pytest.approx(2.0), "must not include bot-b"


class _Venue:
    """Spot venue reporting an account-wide balance, as ccxt does."""

    def __init__(self, account_qty: float) -> None:
        self.account_qty = account_qty
        self.closed: list = []
        self.orders: list = []

    def place_order(self, order):
        from tradingbot.models import OrderResult
        self.orders.append(order)
        return OrderResult(ok=True, order_id="v1", status="closed",
                           filled_qty=order.qty, raw={"average": 100.0})

    def close_position(self, symbol):
        from tradingbot.models import OrderResult
        self.closed.append(symbol)
        return OrderResult(ok=True, order_id="c1", status="closed",
                           filled_qty=self.account_qty, raw={})

    def get_position(self, symbol):
        from tradingbot.models import Position, PositionSide
        if self.account_qty <= 0:
            return None
        return Position(symbol=symbol, side=PositionSide.long,
                        size=self.account_qty, entry_price=0.0)

    def health_check(self):
        return True


class TestSupervisorOwnership:
    """The bot's reported position must be its own, not the account's."""

    def _bot(self, account_qty: float, *event_groups):
        from tradingbot.service.supervisor import BotConfig, BotInstance
        cfg = BotConfig(
            id="bot-a", venue="coinbase", market_type="spot", strategy="example",
            symbol="BTC/USD", timeframe="1m", quantity=0.1, live=True,
            per_bot_cap=1e9, global_cap=1e9, params={},
        )
        bot = BotInstance(config=cfg)
        bot.venue = _Venue(account_qty)
        for group in event_groups:
            for event in group:
                bot.ledger.apply(event)
        return bot

    def test_a_bot_that_bought_nothing_reports_flat_despite_a_balance(self):
        """The account holds 10 BTC someone else bought. This bot owns none."""
        from tradingbot.service.supervisor import BotSupervisor
        bot = self._bot(10.0)

        BotSupervisor._refresh_position(None, bot)  # type: ignore[arg-type]

        assert bot.position is None or bot.position.size == 0.0

    def test_a_bot_reports_only_what_it_bought(self):
        from tradingbot.service.supervisor import BotSupervisor
        bot = self._bot(10.0, _buy("c1", 2.0, 100.0))

        BotSupervisor._refresh_position(None, bot)  # type: ignore[arg-type]

        assert bot.position is not None
        assert bot.position.size == pytest.approx(2.0), "not the account's 10"

    def test_the_reported_entry_price_is_the_cost_basis(self):
        """Was hardcoded to 0.0, which made PnL the whole market value."""
        from tradingbot.service.supervisor import BotSupervisor
        bot = self._bot(10.0, _buy("c1", 2.0, 100.0))

        BotSupervisor._refresh_position(None, bot)  # type: ignore[arg-type]

        assert bot.position is not None
        assert bot.position.entry_price == pytest.approx(100.0)
