from doubles import FakeVenue
from tradingbot.models import Order, PositionSide, Side, OrderType


def test_buy_opens_long_then_close_flattens():
    v = FakeVenue()
    r = v.place_order(Order(symbol="BTCUSDT", side=Side.buy, order_type=OrderType.market, qty=0.01))
    assert r.ok
    pos = v.get_position("BTCUSDT")
    assert pos is not None and pos.side is PositionSide.long
    assert v.close_position("BTCUSDT").ok
    pos = v.get_position("BTCUSDT")
    assert pos is not None and pos.side is PositionSide.flat


def test_records_orders_and_healthcheck():
    v = FakeVenue()
    v.place_order(Order(symbol="BTCUSDT", side=Side.sell, order_type=OrderType.market, qty=0.02))
    assert v.health_check() is True
    assert len(v.orders) == 1
    pos = v.get_position("BTCUSDT")
    assert pos is not None and pos.side is PositionSide.short


def test_close_with_no_position_is_noop():
    v = FakeVenue()
    assert v.get_position("BTCUSDT") is None
    r = v.close_position("BTCUSDT")
    assert r.ok
    assert r.status == "no position"
    assert r.order_id is None
    assert v.orders == []
