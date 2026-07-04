from ..models import Order, OrderResult, OrderType, Position, PositionSide, Side


class BybitTestnetVenue:
    def __init__(self, client, category: str = "linear") -> None:
        self._client = client
        self._category = category

    @classmethod
    def from_credentials(cls, api_key: str, api_secret: str,
                         testnet: bool = True, category: str = "linear") -> "BybitTestnetVenue":
        from pybit.unified_trading import HTTP
        client = HTTP(testnet=testnet, api_key=api_key, api_secret=api_secret)
        return cls(client, category=category)

    def place_order(self, order: Order) -> OrderResult:
        try:
            resp = self._client.place_order(
                category=self._category,
                symbol=order.symbol,
                side="Buy" if order.side is Side.buy else "Sell",
                orderType="Market" if order.order_type is OrderType.market else "Limit",
                qty=str(order.qty),
                price=None if order.price is None else str(order.price),
                reduceOnly=order.reduce_only,
            )
        except Exception as exc:
            return OrderResult(ok=False, order_id=None, status="error",
                               filled_qty=0.0, raw={}, error=str(exc))
        ok = resp.get("retCode") == 0
        return OrderResult(
            ok=ok,
            order_id=(resp.get("result") or {}).get("orderId"),
            status=resp.get("retMsg", ""),
            filled_qty=0.0,
            raw=resp,
            error=None if ok else resp.get("retMsg"),
        )

    def get_position(self, symbol: str) -> Position | None:
        resp = self._client.get_positions(category=self._category, symbol=symbol)
        rows = (resp.get("result") or {}).get("list") or []
        if not rows:
            return None
        row = rows[0]
        size = float(row.get("size") or 0)
        if size == 0:
            return Position(symbol=symbol, side=PositionSide.flat, size=0.0, entry_price=0.0)
        side = PositionSide.long if row.get("side") == "Buy" else PositionSide.short
        return Position(symbol=symbol, side=side, size=size,
                        entry_price=float(row.get("avgPrice") or 0))

    def close_position(self, symbol: str) -> OrderResult:
        pos = self.get_position(symbol)
        if pos is None or pos.side is PositionSide.flat or pos.size == 0:
            return OrderResult(ok=True, order_id=None, status="no position",
                               filled_qty=0.0, raw={})
        close_side = Side.sell if pos.side is PositionSide.long else Side.buy
        return self.place_order(Order(symbol=symbol, side=close_side,
                                      order_type=OrderType.market, qty=pos.size,
                                      reduce_only=True))

    def health_check(self) -> bool:
        try:
            resp = self._client.get_wallet_balance(accountType="UNIFIED")
            return resp.get("retCode") == 0
        except Exception:
            return False
