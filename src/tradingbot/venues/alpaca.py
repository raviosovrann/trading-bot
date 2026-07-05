from __future__ import annotations

from typing import Any

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import OrderSide as AlpacaOrderSide
    from alpaca.trading.enums import TimeInForce
    from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
except Exception:  # pragma: no cover - depends on optional third-party install
    TradingClient = None
    AlpacaOrderSide = None
    TimeInForce = None
    LimitOrderRequest = None
    MarketOrderRequest = None

from ..models import Order, OrderResult, OrderType, Position, PositionSide, Side

_FLAT_TOL = 1e-9


def _raw_dict(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dumped if isinstance(dumped, dict) else {"value": dumped}
    if hasattr(value, "__dict__"):
        return {k: v for k, v in vars(value).items() if not k.startswith("_")}
    return {"value": value}


def _get_attr(obj: Any, *names: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        for name in names:
            if name in obj:
                return obj[name]
        return default
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_not_found_error(exc: Exception) -> bool:
    text = str(exc).lower()
    if "not found" in text or "404" in text:
        return True
    status_code = getattr(exc, "status_code", None)
    return status_code == 404


class AlpacaVenue:
    def __init__(self, client: Any | None = None) -> None:
        if client is None:
            raise ValueError("AlpacaVenue requires a client or use from_credentials(...)")
        self._client = client

    @classmethod
    def from_credentials(cls, api_key: str, api_secret: str, paper: bool = True) -> "AlpacaVenue":
        if TradingClient is None:
            raise RuntimeError("alpaca-py is not installed")
        client = TradingClient(api_key=api_key, secret_key=api_secret, paper=paper)
        return cls(client=client)

    def place_order(self, order: Order) -> OrderResult:
        try:
            if AlpacaOrderSide is not None:
                side: Any = AlpacaOrderSide.BUY if order.side is Side.buy else AlpacaOrderSide.SELL
            else:
                side = order.side.value

            if order.order_type is OrderType.market:
                if MarketOrderRequest is not None and TimeInForce is not None:
                    request = MarketOrderRequest(
                        symbol=order.symbol,
                        qty=order.qty,
                        side=side,
                        time_in_force=TimeInForce.GTC,
                    )
                else:
                    request = {
                        "symbol": order.symbol,
                        "qty": order.qty,
                        "side": side,
                        "type": "market",
                        "time_in_force": "gtc",
                    }
            else:
                if order.price is None:
                    raise ValueError("limit order requires price")
                if LimitOrderRequest is not None and TimeInForce is not None:
                    request = LimitOrderRequest(
                        symbol=order.symbol,
                        qty=order.qty,
                        side=side,
                        limit_price=order.price,
                        time_in_force=TimeInForce.GTC,
                    )
                else:
                    request = {
                        "symbol": order.symbol,
                        "qty": order.qty,
                        "side": side,
                        "type": "limit",
                        "limit_price": order.price,
                        "time_in_force": "gtc",
                    }

            response = self._client.submit_order(order_data=request)
            status = str(_get_attr(response, "status", default="submitted")).lower()
            filled_qty = _to_float(_get_attr(response, "filled_qty", "filled_size", default=order.qty), default=0.0)
            order_id = _get_attr(response, "id", "order_id", "client_order_id")
            error = _get_attr(response, "error", "message")

            ok = status not in {"rejected", "canceled", "cancelled", "failed", "error"}
            if error:
                ok = False

            return OrderResult(
                ok=ok,
                order_id=str(order_id) if order_id is not None else None,
                status=status,
                filled_qty=filled_qty,
                raw=_raw_dict(response),
                error=str(error) if error else None,
            )
        except Exception as exc:
            return OrderResult(
                ok=False,
                order_id=None,
                status="error",
                filled_qty=0.0,
                raw={},
                error=str(exc),
            )

    def get_position(self, symbol: str) -> Position | None:
        try:
            response = self._client.get_open_position(symbol)
        except Exception as exc:
            if _is_not_found_error(exc):
                return None
            return None

        raw_qty = _get_attr(response, "qty", "quantity", "size", default=0)
        size = abs(_to_float(raw_qty, default=0.0))
        if size < _FLAT_TOL:
            return None

        raw_side = str(_get_attr(response, "side", default="")).lower()
        if raw_side in {"long", "buy"}:
            side = PositionSide.long
        elif raw_side in {"short", "sell"}:
            side = PositionSide.short
        else:
            side = PositionSide.long if _to_float(raw_qty, default=0.0) >= 0 else PositionSide.short

        entry_price = _to_float(_get_attr(response, "avg_entry_price", "entry_price", default=0.0), default=0.0)
        return Position(symbol=symbol, side=side, size=size, entry_price=entry_price)

    def close_position(self, symbol: str) -> OrderResult:
        try:
            pos = self.get_position(symbol)
            if pos is None or pos.side is PositionSide.flat or pos.size < _FLAT_TOL:
                return OrderResult(
                    ok=True,
                    order_id=None,
                    status="no position",
                    filled_qty=0.0,
                    raw={},
                    error=None,
                )

            close_side = Side.sell if pos.side is PositionSide.long else Side.buy
            return self.place_order(
                Order(
                    symbol=symbol,
                    side=close_side,
                    order_type=OrderType.market,
                    qty=pos.size,
                    reduce_only=True,
                )
            )
        except Exception as exc:
            return OrderResult(
                ok=False,
                order_id=None,
                status="error",
                filled_qty=0.0,
                raw={},
                error=str(exc),
            )

    def health_check(self) -> bool:
        try:
            if hasattr(self._client, "get_account"):
                self._client.get_account()
            elif hasattr(self._client, "get_clock"):
                self._client.get_clock()
            else:
                return False
            return True
        except Exception:
            return False
