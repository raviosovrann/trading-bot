from __future__ import annotations

import inspect
import uuid
from typing import Any

try:
    from coinbase.rest import RESTClient
except Exception:  # pragma: no cover - depends on optional third-party install
    RESTClient = None

from ..models import Order, OrderResult, OrderType, Position, PositionSide, Side

_FLAT_TOL = 1e-9
_COINBASE_SANDBOX_BASE_URL = "https://api-public.sandbox.exchange.coinbase.com"


def _raw_dict(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dumped if isinstance(dumped, dict) else {"value": dumped}
    if hasattr(value, "to_dict") and callable(value.to_dict):
        dumped = value.to_dict()
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


def _normalize_symbol(symbol: str) -> str:
    return symbol.replace("/", "-")


def _base_asset(symbol: str) -> str:
    s = symbol.replace("/", "-")
    return s.split("-")[0].upper()


class CoinbaseVenue:
    def __init__(self, client: Any | None = None) -> None:
        if client is None:
            raise ValueError("CoinbaseVenue requires a client or use from_credentials(...)")
        self._client = client

    @classmethod
    def from_credentials(cls, api_key: str, api_secret: str, sandbox: bool = True) -> "CoinbaseVenue":
        if RESTClient is None:
            raise RuntimeError("coinbase-advanced-py is not installed")
        if sandbox:
            # SDK constructor signatures have changed across versions; adapt dynamically.
            kwargs = {"api_key": api_key, "api_secret": api_secret, "base_url": _COINBASE_SANDBOX_BASE_URL}
            try:
                params = inspect.signature(RESTClient).parameters
                if "base_url" in params:
                    return cls(client=RESTClient(**kwargs))
                return cls(client=RESTClient(api_key=api_key, api_secret=api_secret))
            except Exception:
                try:
                    return cls(client=RESTClient(api_key=api_key, api_secret=api_secret, base_url=_COINBASE_SANDBOX_BASE_URL))
                except TypeError:
                    return cls(client=RESTClient(api_key=api_key, api_secret=api_secret))

        return cls(client=RESTClient(api_key=api_key, api_secret=api_secret))

    def place_order(self, order: Order) -> OrderResult:
        product_id = _normalize_symbol(order.symbol)
        client_order_id = uuid.uuid4().hex
        try:
            if hasattr(self._client, "create_order"):
                if order.order_type is OrderType.market:
                    config = {"market_market_ioc": {"base_size": str(order.qty)}}
                else:
                    if order.price is None:
                        raise ValueError("limit order requires price")
                    config = {
                        "limit_limit_gtc": {
                            "base_size": str(order.qty),
                            "limit_price": str(order.price),
                        }
                    }

                response = self._client.create_order(
                    client_order_id=client_order_id,
                    product_id=product_id,
                    side=order.side.value.upper(),
                    order_configuration=config,
                )
            else:
                raise RuntimeError("Coinbase client does not support order creation")

            raw = _raw_dict(response)
            success = _get_attr(response, "success", default=raw.get("success", True))
            status = str(
                _get_attr(
                    response,
                    "status",
                    default=raw.get("status") or raw.get("order_status") or ("submitted" if success else "error"),
                )
            ).lower()
            order_id = _get_attr(response, "order_id", "id", default=raw.get("order_id") or raw.get("id"))
            error = _get_attr(response, "error", "message", default=raw.get("error") or raw.get("message"))
            filled_qty = _to_float(
                _get_attr(response, "filled_size", "filled_qty", default=raw.get("filled_size") or raw.get("filled_qty") or 0.0),
                default=0.0,
            )

            ok = bool(success) and status not in {"rejected", "failed", "error", "canceled", "cancelled"}
            if error:
                ok = False

            return OrderResult(
                ok=ok,
                order_id=str(order_id) if order_id is not None else None,
                status=status,
                filled_qty=filled_qty,
                raw=raw,
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
            if hasattr(self._client, "get_position"):
                response = self._client.get_position(product_id=_normalize_symbol(symbol))
                size = abs(
                    _to_float(
                        _get_attr(response, "size", "qty", "quantity", "base_size", default=0.0),
                        default=0.0,
                    )
                )
                if size < _FLAT_TOL:
                    return None

                raw_side = str(_get_attr(response, "side", default="")).lower()
                if raw_side in {"long", "buy"}:
                    side = PositionSide.long
                elif raw_side in {"short", "sell"}:
                    side = PositionSide.short
                elif raw_side == "flat":
                    side = PositionSide.flat
                else:
                    signed = _to_float(_get_attr(response, "size", "qty", default=0.0), default=0.0)
                    side = PositionSide.long if signed >= 0 else PositionSide.short

                if side is PositionSide.flat:
                    return None
                entry_price = _to_float(
                    _get_attr(response, "entry_price", "avg_entry_price", default=0.0),
                    default=0.0,
                )
                return Position(symbol=symbol, side=side, size=size, entry_price=entry_price)

            if hasattr(self._client, "get_accounts"):
                accounts = self._client.get_accounts()
                raw_accounts = _get_attr(accounts, "accounts", default=accounts)
                if not isinstance(raw_accounts, list):
                    raw_accounts = []

                target_currency = _base_asset(symbol)
                for acct in raw_accounts:
                    currency = str(_get_attr(acct, "currency", default="")).upper()
                    if currency != target_currency:
                        continue
                    balance = _get_attr(acct, "available_balance", "balance", default={})
                    size = abs(
                        _to_float(
                            _get_attr(balance, "value", "amount", default=balance),
                            default=0.0,
                        )
                    )
                    if size < _FLAT_TOL:
                        return None
                    return Position(symbol=symbol, side=PositionSide.long, size=size, entry_price=0.0)
        except Exception:
            return None
        return None

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
            if hasattr(self._client, "get_accounts"):
                self._client.get_accounts()
                return True
            if hasattr(self._client, "get_account"):
                self._client.get_account()
                return True
            if hasattr(self._client, "get_portfolios"):
                self._client.get_portfolios()
                return True
            return False
        except Exception:
            return False
