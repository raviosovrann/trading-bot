import pytest
from tradingbot.models import Signal, Action, OrderType, PositionSide
from tradingbot.parser import parse_signal, SignalParseError


def _valid_payload():
    return {
        "token": "secret",
        "strategy": "btc-futures-v1",
        "action": "buy",
        "symbol": "BTCUSDT",
        "order_type": "market",
        "price": 61250.5,
        "quantity": 0.01,
        "position_side": "long",
        "time": "1720000000",
    }


def test_parse_valid_signal():
    sig = parse_signal(_valid_payload())
    assert isinstance(sig, Signal)
    assert sig.action is Action.buy
    assert sig.order_type is OrderType.market
    assert sig.position_side is PositionSide.long
    assert sig.symbol == "BTCUSDT"
    assert sig.quantity == 0.01


def test_order_type_defaults_to_market():
    payload = _valid_payload()
    del payload["order_type"]
    assert parse_signal(payload).order_type is OrderType.market


def test_missing_required_field_raises():
    payload = _valid_payload()
    del payload["action"]
    with pytest.raises(SignalParseError):
        parse_signal(payload)


def test_invalid_action_raises():
    payload = _valid_payload()
    payload["action"] = "hodl"
    with pytest.raises(SignalParseError):
        parse_signal(payload)


def test_non_positive_quantity_raises():
    payload = _valid_payload()
    payload["quantity"] = 0
    with pytest.raises(SignalParseError):
        parse_signal(payload)
