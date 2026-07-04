from tradingbot.models import Order, OrderType, Side, PositionSide
from tradingbot.venues.alpaca import AlpacaVenue


class _Obj:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def test_place_order_success_mapping():
    class Client:
        def submit_order(self, order_data):
            return _Obj(id="alp-1", status="accepted", filled_qty="0.01")

    venue = AlpacaVenue(client=Client())
    result = venue.place_order(Order(symbol="BTC/USD", side=Side.buy, order_type=OrderType.market, qty=0.01))

    assert result.ok is True
    assert result.order_id == "alp-1"
    assert result.status == "accepted"
    assert result.filled_qty == 0.01


def test_place_order_exception_returns_structured_failure():
    class Client:
        def submit_order(self, order_data):
            raise RuntimeError("submit failed")

    venue = AlpacaVenue(client=Client())
    result = venue.place_order(Order(symbol="BTC/USD", side=Side.buy, order_type=OrderType.market, qty=0.01))

    assert result.ok is False
    assert result.status == "error"
    assert "submit failed" in (result.error or "")


def test_get_position_long_mapping():
    class Client:
        def get_open_position(self, symbol):
            return _Obj(side="long", qty="0.5", avg_entry_price="60000")

    venue = AlpacaVenue(client=Client())
    pos = venue.get_position("BTC/USD")

    assert pos is not None
    assert pos.side is PositionSide.long
    assert pos.size == 0.5
    assert pos.entry_price == 60000.0


def test_close_position_noop_when_flat_or_none():
    class Client:
        def __init__(self):
            self.submit_calls = 0

        def get_open_position(self, symbol):
            return _Obj(side="flat", qty="0", avg_entry_price="0")

        def submit_order(self, order_data):
            self.submit_calls += 1
            return _Obj(id="never", status="accepted", filled_qty="0")

    client = Client()
    venue = AlpacaVenue(client=client)
    result = venue.close_position("BTC/USD")

    assert result.ok is True
    assert result.status == "no position"
    assert result.order_id is None
    assert client.submit_calls == 0


def test_health_check_true_and_false_paths():
    class OkClient:
        def get_account(self):
            return _Obj(id="acct")

    class BadClient:
        def get_account(self):
            raise RuntimeError("down")

    assert AlpacaVenue(client=OkClient()).health_check() is True
    assert AlpacaVenue(client=BadClient()).health_check() is False
