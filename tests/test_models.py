import pytest
from pydantic import ValidationError

from tradingbot.models import Signal, Action, OrderType, PositionSide


def _valid():
    return {
        "strategy": "btc-futures-v1",
        "action": "buy",
        "symbol": "BTCUSDT",
        "order_type": "market",
        "price": 61250.5,
        "quantity": 0.01,
        "position_side": "long",
    }


def test_parse_valid_signal():
    sig = Signal.model_validate(_valid())
    assert sig.action is Action.buy
    assert sig.order_type is OrderType.market
    assert sig.position_side is PositionSide.long
    assert sig.quantity == 0.01


def test_order_type_defaults_to_market():
    p = _valid(); del p["order_type"]
    assert Signal.model_validate(p).order_type is OrderType.market


def test_missing_required_field_raises():
    p = _valid(); del p["action"]
    with pytest.raises(ValidationError):
        Signal.model_validate(p)


def test_invalid_action_raises():
    p = _valid(); p["action"] = "hodl"
    with pytest.raises(ValidationError):
        Signal.model_validate(p)


def test_non_positive_quantity_raises():
    p = _valid(); p["quantity"] = 0
    with pytest.raises(ValidationError):
        Signal.model_validate(p)


def test_nan_quantity_raises():
    p = _valid(); p["quantity"] = float("nan")
    with pytest.raises(ValidationError):
        Signal.model_validate(p)


def test_infinite_quantity_raises():
    p = _valid(); p["quantity"] = float("inf")
    with pytest.raises(ValidationError):
        Signal.model_validate(p)


def test_limit_without_price_raises():
    p = _valid(); p["order_type"] = "limit"; del p["price"]
    with pytest.raises(ValidationError):
        Signal.model_validate(p)


def test_limit_with_price_ok():
    p = _valid(); p["order_type"] = "limit"; p["price"] = 61000.0
    sig = Signal.model_validate(p)
    assert sig.order_type is OrderType.limit and sig.price == 61000.0
