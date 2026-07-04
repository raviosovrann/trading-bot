import pytest

from tradingbot.models import Candle, Side, Order, OrderResult, Position, OrderType, PositionSide


def test_candle_fields():
    c = Candle(timestamp=1000, open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0)
    assert c.close == 1.5 and c.high == 2.0


def test_order_defaults():
    o = Order(symbol="BTCUSDT", side=Side.buy, order_type=OrderType.market, qty=0.01)
    assert o.reduce_only is False and o.price is None and o.side is Side.buy


def test_order_result_and_position():
    r = OrderResult(ok=True, order_id="1", status="ok", filled_qty=0.01, raw={})
    assert r.ok and r.error is None
    p = Position(symbol="BTCUSDT", side=PositionSide.long, size=0.01, entry_price=60000.0)
    assert p.side is PositionSide.long


def test_position_rejects_invalid_side():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Position(symbol="BTCUSDT", side="sideways", size=1.0, entry_price=1.0)
