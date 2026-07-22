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
    buy_pos = CcxtVenue(
        _FakeFuturesExchange([{"symbol": "S", "side": "buy", "contracts": 1}]), market_type="futures"
    ).get_position("S")
    assert buy_pos is not None and buy_pos.side is PositionSide.long
    sell_pos = CcxtVenue(
        _FakeFuturesExchange([{"symbol": "S", "side": "sell", "contracts": 1}]), market_type="futures"
    ).get_position("S")
    assert sell_pos is not None and sell_pos.side is PositionSide.short
    neg = _FakeFuturesExchange([{"symbol": "S", "contracts": -2}])  # no side, signed
    p = CcxtVenue(neg, market_type="futures").get_position("S")
    assert p is not None and p.side is PositionSide.short and p.size == 2


class _FetchExchange:
    """Fake exposing only fetch_order, for order-status polling (#135)."""

    def __init__(self, response=None, raises=False):
        self._response = response
        self._raises = raises
        self.calls = []

    def fetch_order(self, order_id, symbol):
        self.calls.append((order_id, symbol))
        if self._raises:
            raise RuntimeError("exchange unreachable")
        return self._response


def test_fetch_order_reports_cumulative_fill_state():
    """ccxt reports `filled` for the whole order, not per execution."""
    exchange = _FetchExchange({"id": "v1", "status": "closed", "filled": 2.0,
                               "average": 150.0})
    venue = CcxtVenue(exchange, live=True)

    result = venue.fetch_order("v1", "BTC/USD")

    assert exchange.calls == [("v1", "BTC/USD")]
    assert result.ok is True
    assert result.status == "closed"
    assert result.filled_qty == 2.0
    assert result.raw["average"] == 150.0


def test_fetch_order_reports_a_partial_fill():
    exchange = _FetchExchange({"id": "v1", "status": "open", "filled": 0.5})
    venue = CcxtVenue(exchange, live=True)

    result = venue.fetch_order("v1", "BTC/USD")

    assert result.ok is True
    assert result.status == "open"
    assert result.filled_qty == 0.5


def test_fetch_order_treats_an_unreachable_exchange_as_unknown_not_failed():
    """The caller must not read this as a rejection.

    An exchange we cannot reach has told us nothing about the order. Marking
    it dead here would cancel a live order in our books while it keeps
    working at the venue.
    """
    venue = CcxtVenue(_FetchExchange(raises=True), live=True)

    result = venue.fetch_order("v1", "BTC/USD")

    assert result.ok is False
    assert result.status == "error"
    assert result.filled_qty == 0.0
    assert result.error is not None


def test_fetch_order_defaults_a_missing_status_to_open():
    """An order with no status is still live until something says otherwise."""
    venue = CcxtVenue(_FetchExchange({"id": "v1", "filled": 0.0}), live=True)

    assert venue.fetch_order("v1", "BTC/USD").status == "open"


def test_fetch_order_tolerates_a_missing_filled_field():
    venue = CcxtVenue(_FetchExchange({"id": "v1", "status": "open"}), live=True)

    assert venue.fetch_order("v1", "BTC/USD").filled_qty == 0.0


class _MarketExchange:
    """Fake exposing ccxt market metadata for contract resolution (#124)."""

    def __init__(self, markets):
        self.markets = markets
        self.load_calls = 0

    def load_markets(self, reload=False):
        del reload
        self.load_calls += 1
        return self.markets


_SPOT_MARKET = {
    "symbol": "BTC/USD", "base": "BTC", "quote": "USD", "settle": "USD",
    "contract": False, "spot": True, "precision": {"price": 0.01},
}
_PERP_MARKET = {
    "symbol": "BTC/USD:USD", "base": "BTC", "quote": "USD", "settle": "USD",
    "contract": True, "swap": True, "linear": True, "inverse": False,
    "contractSize": 0.001, "precision": {"price": 0.1},
}


def test_contract_spec_resolves_a_spot_market():
    venue = CcxtVenue(_MarketExchange({"BTC/USD": _SPOT_MARKET}), live=True)

    spec = venue.contract_spec("BTC/USD")

    assert spec.contract_size == 1.0
    assert spec.is_derivative is False


def test_contract_spec_resolves_a_derivative_from_venue_metadata():
    venue = CcxtVenue(
        _MarketExchange({"BTC/USD:USD": _PERP_MARKET}), live=True, market_type="futures"
    )

    spec = venue.contract_spec("BTC/USD:USD")

    assert spec.contract_size == 0.001
    assert spec.is_derivative is True


def test_contract_spec_refuses_a_derivative_with_no_published_size():
    """#124's core: never silently 1.0 for a derivative."""
    from tradingbot.venues.contracts import ContractMetadataError

    market = {**_PERP_MARKET, "contractSize": None}
    venue = CcxtVenue(
        _MarketExchange({"BTC/USD:USD": market}), live=True, market_type="futures"
    )

    with pytest.raises(ContractMetadataError):
        venue.contract_spec("BTC/USD:USD")


def test_contract_spec_refuses_an_unlisted_symbol():
    from tradingbot.venues.contracts import ContractMetadataError

    venue = CcxtVenue(_MarketExchange({}), live=True, market_type="futures")

    with pytest.raises(ContractMetadataError):
        venue.contract_spec("NOPE/USD:USD")


def test_contract_spec_caches_across_calls():
    """Metadata must not become a per-order exchange round trip."""
    exchange = _MarketExchange({"BTC/USD": _SPOT_MARKET})
    venue = CcxtVenue(exchange, live=True)

    venue.contract_spec("BTC/USD")
    venue.contract_spec("BTC/USD")

    assert exchange.load_calls == 1
