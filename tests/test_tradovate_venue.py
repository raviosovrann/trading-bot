"""Tests for the Tradovate venue integration."""

import pytest

from tradingbot.models import Order, OrderType, PositionSide, Side
from tradingbot.venues.tradovate import TradovateVenue


class _FakeClient:
    def __init__(self, place_result=None, positions=None, account_raises=False):
        self.place_result = place_result if place_result is not None else {"orderId": 111}
        self.positions = positions or []
        self.account_raises = account_raises
        self.calls = []

    def place_order(self, account_id, account_spec, action, symbol, qty,
                    order_type, price=None, reduce_only=False):
        self.calls.append((action, symbol, qty, order_type, price, reduce_only))
        return self.place_result

    def list_positions(self, account_id):
        return list(self.positions)

    def account(self):
        if self.account_raises:
            raise RuntimeError("auth failed")
        return {"id": 1, "name": "DEMO123"}


def _venue(client, *, live=True):
    return TradovateVenue(client, account_id=1, account_spec="DEMO123", live=live)


# --- construction ---------------------------------------------------------- #

def test_construct_requires_client():
    """Verify that TradovateVenue requires a client."""
    with pytest.raises(ValueError):
        TradovateVenue(None)


# --- place_order: dry-run guard ------------------------------------------- #

def test_dry_run_does_not_call_client():
    """Verify that dry-run mode does not call the Tradovate client."""
    client = _FakeClient()
    venue = _venue(client, live=False)
    r = venue.place_order(Order(symbol="MBTF6", side=Side.buy, order_type=OrderType.market, qty=1))
    assert r.ok is True
    assert r.status == "dry_run"
    assert client.calls == []


# --- place_order: live path (long/short/limit/failure/exception) ----------- #

def test_live_market_buy_maps_ok_and_sends_buy():
    """Verify that a live market buy maps to a buy order sent to the client."""
    client = _FakeClient(place_result={"orderId": 555})
    venue = _venue(client, live=True)
    r = venue.place_order(Order(symbol="MBTF6", side=Side.buy, order_type=OrderType.market, qty=2))
    assert r.ok is True and r.order_id == "555" and r.status == "submitted"
    assert client.calls == [("Buy", "MBTF6", 2, "Market", None, False)]


def test_live_market_sell_sends_sell_for_short():
    """Verify that a live market sell sends a sell order to the client."""
    client = _FakeClient(place_result={"orderId": 7})
    venue = _venue(client, live=True)
    venue.place_order(Order(symbol="MBTF6", side=Side.sell, order_type=OrderType.market, qty=1))
    assert client.calls[0][0] == "Sell"  # opens/adds a short on futures


def test_live_limit_passes_price():
    """Verify that a live limit order passes the price to the client."""
    client = _FakeClient(place_result={"orderId": 9})
    venue = _venue(client, live=True)
    venue.place_order(Order(symbol="MBTF6", side=Side.buy, order_type=OrderType.limit, qty=1, price=64000.0))
    assert client.calls[0][3] == "Limit" and client.calls[0][4] == 64000.0


def test_live_failure_returns_not_ok():
    """Verify that a failure response from the client maps to a non-ok result."""
    client = _FakeClient(place_result={"failureReason": "InsufficientMargin", "failureText": "no funds"})
    venue = _venue(client, live=True)
    r = venue.place_order(Order(symbol="MBTF6", side=Side.buy, order_type=OrderType.market, qty=1))
    assert r.ok is False and r.status == "rejected" and "no funds" in (r.error or "")


def test_live_without_account_returns_error_and_sends_nothing():
    """Verify that missing account details return an error and send nothing."""
    client = _FakeClient(place_result={"orderId": 1})
    venue = TradovateVenue(client, account_id=None, account_spec=None, live=True)
    r = venue.place_order(Order(symbol="MBTF6", side=Side.buy, order_type=OrderType.market, qty=1))
    assert r.ok is False and r.status == "error" and "account" in (r.error or "").lower()
    assert client.calls == []  # never sent a malformed request


def test_live_client_exception_returns_error():
    """Verify that client exceptions during order placement are returned as an error."""
    class _Boom(_FakeClient):
        def place_order(self, *a, **k):
            raise RuntimeError("network down")

    venue = _venue(_Boom(), live=True)
    r = venue.place_order(Order(symbol="MBTF6", side=Side.buy, order_type=OrderType.market, qty=1))
    assert r.ok is False and r.status == "error" and "network down" in (r.error or "")


# --- get_position: long/short/flat ---------------------------------------- #

def test_get_position_long_from_positive_netpos():
    """Verify that a positive net position maps to a long position."""
    client = _FakeClient(positions=[{"symbol": "MBTF6", "netPos": 3, "netPrice": 64000.0}])
    pos = _venue(client).get_position("MBTF6")
    assert pos is not None and pos.side is PositionSide.long
    assert pos.size == 3 and pos.entry_price == 64000.0


def test_get_position_short_from_negative_netpos():
    """Verify that a negative net position maps to a short position."""
    client = _FakeClient(positions=[{"symbol": "MBTF6", "netPos": -2}])
    pos = _venue(client).get_position("MBTF6")
    assert pos is not None and pos.side is PositionSide.short and pos.size == 2


def test_get_position_flat_or_absent_returns_none():
    """Verify that a flat or absent position returns None."""
    assert _venue(_FakeClient(positions=[])).get_position("MBTF6") is None
    zero = _FakeClient(positions=[{"symbol": "MBTF6", "netPos": 0}])
    assert _venue(zero).get_position("MBTF6") is None


# --- close_position -------------------------------------------------------- #

def test_close_long_sells_size_reduce_only():
    """Verify that closing a long sells the position size with reduce-only."""
    client = _FakeClient(positions=[{"symbol": "MBTF6", "netPos": 3}], place_result={"orderId": 1})
    r = _venue(client, live=True).close_position("MBTF6")
    assert r.ok is True
    assert client.calls == [("Sell", "MBTF6", 3, "Market", None, True)]


def test_close_short_buys_size_reduce_only():
    """Verify that closing a short buys the position size with reduce-only."""
    client = _FakeClient(positions=[{"symbol": "MBTF6", "netPos": -2}], place_result={"orderId": 1})
    _venue(client, live=True).close_position("MBTF6")
    assert client.calls[0][0] == "Buy" and client.calls[0][5] is True


def test_close_when_flat_is_noop():
    """Verify that closing a flat position is a no-op."""
    client = _FakeClient(positions=[])
    r = _venue(client, live=True).close_position("MBTF6")
    assert r.ok is True and r.status == "no position" and client.calls == []


# --- health + contract multiplier ----------------------------------------- #

def test_health_check_true_then_false():
    """Verify that health check reflects the client's account availability."""
    assert _venue(_FakeClient()).health_check() is True
    assert _venue(_FakeClient(account_raises=True)).health_check() is False


def test_contract_multiplier_micro_and_standard():
    """Verify contract multiplier values for micro and standard contracts."""
    v = _venue(_FakeClient())
    assert v.contract_multiplier("MBTF6") == 0.1    # Micro Bitcoin = 0.1 BTC
    assert v.contract_multiplier("METF6") == 0.1    # Micro Ether = 0.1 ETH
    assert v.contract_multiplier("BTCF6") == 5.0    # Bitcoin (full) = 5 BTC
    assert v.contract_multiplier("ETHF6") == 50.0   # Ether (full) = 50 ETH
    assert v.contract_multiplier("UNKNOWN") == 1.0  # safe default


# --- from_credentials offline guard --------------------------------------- #

def test_from_credentials_requires_httpx(monkeypatch):
    """Verify that from_credentials requires httpx to be available."""
    import tradingbot.venues.tradovate as tv

    monkeypatch.setattr(tv, "httpx", None)
    with pytest.raises(RuntimeError):
        tv.TradovateVenue.from_credentials(
            name="u", password="p", app_id="a", app_version="1", cid="1", sec="s",
        )
