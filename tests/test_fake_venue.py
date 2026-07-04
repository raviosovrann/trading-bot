from tradingbot.venues.fake import FakeVenue
from tradingbot.models import Order, Side, OrderType


def test_buy_opens_long_then_close_flattens():
    v = FakeVenue()
    r = v.place_order(Order(symbol="BTCUSDT", side=Side.buy, order_type=OrderType.market, qty=0.01))
    assert r.ok
    assert v.get_position("BTCUSDT").side == "long"
    assert v.close_position("BTCUSDT").ok
    assert v.get_position("BTCUSDT").side == "flat"


def test_records_orders_and_healthcheck():
    v = FakeVenue()
    v.place_order(Order(symbol="BTCUSDT", side=Side.sell, order_type=OrderType.market, qty=0.02))
    assert v.health_check() is True
    assert len(v.orders) == 1
    assert v.get_position("BTCUSDT").side == "short"
