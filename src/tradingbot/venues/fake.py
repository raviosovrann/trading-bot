from ..models import Order, OrderResult, OrderType, Position, Side


class FakeVenue:
    """In-memory venue for tests: one net position per symbol."""

    def __init__(self) -> None:
        self.orders: list[Order] = []
        self._net: dict[str, float] = {}

    def place_order(self, order: Order) -> OrderResult:
        self.orders.append(order)
        delta = order.qty if order.side is Side.buy else -order.qty
        self._net[order.symbol] = self._net.get(order.symbol, 0.0) + delta
        return OrderResult(ok=True, order_id=str(len(self.orders)), status="filled",
                           filled_qty=order.qty, raw={})

    def get_position(self, symbol: str) -> Position | None:
        net = self._net.get(symbol, 0.0)
        side = "flat" if net == 0 else ("long" if net > 0 else "short")
        return Position(symbol=symbol, side=side, size=abs(net), entry_price=0.0)

    def close_position(self, symbol: str) -> OrderResult:
        pos = self.get_position(symbol)
        if pos is None or pos.side == "flat":
            return OrderResult(ok=True, order_id=None, status="no position",
                               filled_qty=0.0, raw={})
        close_side = Side.sell if pos.side == "long" else Side.buy
        return self.place_order(Order(symbol=symbol, side=close_side,
                                      order_type=OrderType.market, qty=pos.size,
                                      reduce_only=True))

    def health_check(self) -> bool:
        return True
