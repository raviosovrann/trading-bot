"""Tests for CcxtVenue (spot, LIVE-guard, futures-ready)."""

import pytest

from tradingbot.models import Order, OrderType, PositionSide, Side
from tradingbot.venues.ccxt import CcxtVenue


class _FakeExchange:
    """Minimal no-network fake of a ccxt exchange client."""

    def __init__(self, balance=None, create_order_return=None, raise_on_create=False,
                 raise_on_balance=False):
        self._balance = balance or {}
        self._create_order_return = create_order_return or {
            "id": "abc123",
            "status": "closed",
            "filled": 1.0,
            "info": {},
        }
        self._raise_on_create = raise_on_create
        self._raise_on_balance = raise_on_balance
        self.create_order_calls = []

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        self.create_order_calls.append(
            {"symbol": symbol, "type": type, "side": side, "amount": amount, "price": price}
        )
        if self._raise_on_create:
            raise RuntimeError("boom")
        return self._create_order_return

    def fetch_balance(self):
        if self._raise_on_balance:
            raise RuntimeError("balance boom")
        return self._balance


def test_init_requires_exchange():
    """Verify that CcxtVenue requires an exchange."""
    with pytest.raises(ValueError):
        CcxtVenue(None)


def test_live_market_buy_calls_create_order_once():
    """Verify that a live market buy calls create_order once."""
    ex = _FakeExchange(create_order_return={"id": "oid1", "status": "closed", "filled": 2.5, "info": {}})
    venue = CcxtVenue(ex, live=True)
    order = Order(symbol="BTC/USD", side=Side.buy, order_type=OrderType.market, qty=2.5)

    result = venue.place_order(order)

    assert len(ex.create_order_calls) == 1
    call = ex.create_order_calls[0]
    assert call["symbol"] == "BTC/USD"
    assert call["type"] == "market"
    assert call["side"] == "buy"
    assert call["amount"] == 2.5
    assert call["price"] is None
    assert result.ok is True
    assert result.status == "closed"
    assert result.filled_qty == 2.5
    assert result.order_id == "oid1"


def test_live_limit_order_passes_price():
    """Verify that a live limit order passes the price to the exchange."""
    ex = _FakeExchange()
    venue = CcxtVenue(ex, live=True)
    order = Order(symbol="BTC/USD", side=Side.buy, order_type=OrderType.limit, qty=1.0, price=100.0)

    venue.place_order(order)

    assert ex.create_order_calls[0]["price"] == 100.0
    assert ex.create_order_calls[0]["type"] == "limit"


def test_dry_run_does_not_call_exchange():
    """Verify that dry-run mode does not call the exchange."""
    ex = _FakeExchange()
    venue = CcxtVenue(ex, live=False)
    order = Order(symbol="BTC/USD", side=Side.buy, order_type=OrderType.market, qty=1.0)

    result = venue.place_order(order)

    assert len(ex.create_order_calls) == 0
    assert result.status == "dry_run"
    assert result.ok is True
    assert result.raw["dry_run"] is True
    assert result.raw["symbol"] == "BTC/USD"


def test_create_order_raises_returns_error_result():
    """Verify that exchange errors during create_order are returned as an error result."""
    ex = _FakeExchange(raise_on_create=True)
    venue = CcxtVenue(ex, live=True)
    order = Order(symbol="BTC/USD", side=Side.buy, order_type=OrderType.market, qty=1.0)

    result = venue.place_order(order)

    assert result.ok is False
    assert result.status == "error"
    assert result.error is not None
    assert "boom" in result.error


def test_rejected_status_maps_to_not_ok():
    """Verify that a rejected order status maps to a non-ok result."""
    ex = _FakeExchange(create_order_return={"id": "x", "status": "rejected", "filled": 0.0, "info": {}})
    venue = CcxtVenue(ex, live=True)
    order = Order(symbol="BTC/USD", side=Side.buy, order_type=OrderType.market, qty=1.0)

    result = venue.place_order(order)

    assert result.ok is False
    assert result.status == "rejected"


def test_get_position_returns_long_from_balance_total():
    """Verify that a positive balance total maps to a long position."""
    ex = _FakeExchange(balance={"BTC": {"free": 0.5, "used": 0.0, "total": 1.5}})
    venue = CcxtVenue(ex, live=True)

    pos = venue.get_position("BTC/USD")

    assert pos is not None
    assert pos.symbol == "BTC/USD"
    assert pos.side is PositionSide.long
    assert pos.size == 1.5
    assert pos.entry_price == 0.0


def test_get_position_zero_balance_returns_none():
    """Verify that a zero balance returns no position."""
    ex = _FakeExchange(balance={"BTC": {"free": 0.0, "used": 0.0, "total": 0.0}})
    venue = CcxtVenue(ex, live=True)

    assert venue.get_position("BTC/USD") is None


def test_get_position_absent_balance_returns_none():
    """Verify that an absent currency balance returns no position."""
    ex = _FakeExchange(balance={})
    venue = CcxtVenue(ex, live=True)

    assert venue.get_position("BTC/USD") is None


def test_get_position_balance_raises_returns_none():
    """Verify that balance fetch errors return no position."""
    ex = _FakeExchange(raise_on_balance=True)
    venue = CcxtVenue(ex, live=True)

    assert venue.get_position("BTC/USD") is None


def test_close_position_live_places_market_sell():
    """Verify that a live close position places a market sell for the balance."""
    ex = _FakeExchange(balance={"BTC": {"free": 0.0, "used": 0.0, "total": 3.0}})
    venue = CcxtVenue(ex, live=True)

    result = venue.close_position("BTC/USD")

    assert len(ex.create_order_calls) == 1
    call = ex.create_order_calls[0]
    assert call["side"] == "sell"
    assert call["type"] == "market"
    assert call["amount"] == 3.0
    assert result.ok is True


def test_close_position_no_balance_no_order():
    """Verify that closing a position with no balance does not place an order."""
    ex = _FakeExchange(balance={})
    venue = CcxtVenue(ex, live=True)

    result = venue.close_position("BTC/USD")

    assert len(ex.create_order_calls) == 0
    assert result.ok is True
    assert result.status == "no position"


def test_close_position_dry_run_does_not_call_exchange():
    """Verify that dry-run close position does not call the exchange."""
    ex = _FakeExchange(balance={"BTC": {"free": 0.0, "used": 0.0, "total": 3.0}})
    venue = CcxtVenue(ex, live=False)

    result = venue.close_position("BTC/USD")

    assert len(ex.create_order_calls) == 0
    assert result.status == "dry_run"


def test_health_check_true_when_balance_ok():
    """Verify that health check succeeds when the balance is available."""
    ex = _FakeExchange(balance={"BTC": {"total": 1.0}})
    venue = CcxtVenue(ex, live=True)

    assert venue.health_check() is True


def test_health_check_false_when_balance_raises():
    """Verify that health check fails when the balance fetch raises."""
    ex = _FakeExchange(raise_on_balance=True)
    venue = CcxtVenue(ex, live=True)

    assert venue.health_check() is False


class _FakeFuturesExchange:
    def __init__(self, positions):
        self._positions = positions
    def fetch_positions(self, symbols):
        return self._positions


def test_futures_get_position_long_and_short():
    ex = _FakeFuturesExchange([{"symbol": "BTC/USD:USD", "side": "long", "contracts": 3, "entryPrice": 64000.0}])
    v = CcxtVenue(ex, market_type="futures")
    pos = v.get_position("BTC/USD:USD")
    assert pos is not None and pos.side is PositionSide.long
    assert pos.size == 3 and pos.entry_price == 64000.0

    ex2 = _FakeFuturesExchange([{"symbol": "BTC/USD:USD", "side": "short", "contracts": 2}])
    short_pos = CcxtVenue(ex2, market_type="futures").get_position("BTC/USD:USD")
    assert short_pos is not None and short_pos.side is PositionSide.short


def test_futures_get_position_flat_returns_none():
    ex = _FakeFuturesExchange([{"symbol": "BTC/USD:USD", "side": "long", "contracts": 0}])
    assert CcxtVenue(ex, market_type="futures").get_position("BTC/USD:USD") is None
    assert CcxtVenue(_FakeFuturesExchange([]), market_type="futures").get_position("BTC/USD:USD") is None


def test_futures_get_position_infers_side_from_buy_sell_and_sign():
    # ccxt normally returns side "long"/"short", but some exchanges use
    # "buy"/"sell" or omit it; fall back to the sign of contracts.
    buy = _FakeFuturesExchange([{"symbol": "S", "side": "buy", "contracts": 1}])
    assert CcxtVenue(buy, market_type="futures").get_position("S").side is PositionSide.long
    sell = _FakeFuturesExchange([{"symbol": "S", "side": "sell", "contracts": 1}])
    assert CcxtVenue(sell, market_type="futures").get_position("S").side is PositionSide.short
    neg = _FakeFuturesExchange([{"symbol": "S", "contracts": -2}])  # no side, signed
    p = CcxtVenue(neg, market_type="futures").get_position("S")
    assert p is not None and p.side is PositionSide.short and p.size == 2
