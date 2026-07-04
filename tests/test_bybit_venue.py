import os

import pytest

from tradingbot.venues.bybit import BybitTestnetVenue
from tradingbot.models import Order, Side, OrderType


class FakeHTTP:
    def __init__(self):
        self.calls = []

    def place_order(self, **kw):
        self.calls.append(("place_order", kw))
        return {"retCode": 0, "retMsg": "OK", "result": {"orderId": "abc"}}

    def get_positions(self, **kw):
        return {"retCode": 0, "result": {"list": [
            {"side": "Buy", "size": "0.01", "avgPrice": "60000"}]}}

    def get_wallet_balance(self, **kw):
        return {"retCode": 0, "result": {}}


def test_place_order_maps_to_pybit():
    http = FakeHTTP()
    r = BybitTestnetVenue(http).place_order(
        Order(symbol="BTCUSDT", side=Side.buy, order_type=OrderType.market, qty=0.01))
    assert r.ok and r.order_id == "abc"
    _, kw = http.calls[0]
    assert kw["symbol"] == "BTCUSDT" and kw["side"] == "Buy"
    assert kw["orderType"] == "Market" and kw["qty"] == "0.01"


def test_get_position_parses_long():
    pos = BybitTestnetVenue(FakeHTTP()).get_position("BTCUSDT")
    assert pos.side == "long" and pos.size == 0.01 and pos.entry_price == 60000.0


def test_health_check_true_on_retcode_zero():
    assert BybitTestnetVenue(FakeHTTP()).health_check() is True


@pytest.mark.skipif(
    not (os.getenv("BYBIT_API_KEY") and os.getenv("BYBIT_API_SECRET")),
    reason="no Bybit testnet credentials in env",
)
def test_live_testnet_health_check():
    v = BybitTestnetVenue.from_credentials(
        os.environ["BYBIT_API_KEY"], os.environ["BYBIT_API_SECRET"], testnet=True)
    assert v.health_check() is True
