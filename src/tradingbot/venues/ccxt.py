"""CCXT-backed execution venue (spot, LIVE-guard, futures-ready)."""

from __future__ import annotations

from ..models import Order, OrderResult, OrderType, Position, PositionSide, Side

try:
    import ccxt  # type: ignore
except Exception:  # pragma: no cover
    ccxt = None  # type: ignore[assignment]


class CcxtVenue:
    """Execution venue backed by a ccxt exchange client.

    Spot only for now. Futures support is a future drop-in via
    ``fetch_positions()`` keyed on ``self._market_type``.
    """

    def __init__(self, exchange=None, *, live: bool = False, market_type: str = "spot"):
        if exchange is None:
            raise ValueError("CcxtVenue requires an exchange or use from_exchange(...)")
        self._ex = exchange
        self._live = live
        self._market_type = market_type

    @classmethod
    def from_exchange(
        cls,
        exchange_id: str,
        api_key: str,
        api_secret: str,
        password: str | None = None,
        *,
        live: bool = False,
        market_type: str = "spot",
    ) -> "CcxtVenue":
        if ccxt is None:
            raise RuntimeError("ccxt is not installed")
        klass = getattr(ccxt, exchange_id)
        config: dict = {"apiKey": api_key, "secret": api_secret, "enableRateLimit": True}
        if password:
            config["password"] = password
        if market_type == "futures":
            # Select the exchange's derivatives markets (perps/futures).
            config["options"] = {"defaultType": "swap"}
        return cls(klass(config), live=live, market_type=market_type)

    def place_order(self, order: Order) -> OrderResult:
        # LIVE GUARD: when not live, never touch the exchange.
        if not self._live:
            return OrderResult(
                ok=True,
                order_id=None,
                status="dry_run",
                filled_qty=0.0,
                raw={
                    "dry_run": True,
                    "symbol": order.symbol,
                    "side": order.side.value,
                    "type": order.order_type.value,
                    "qty": order.qty,
                    "price": order.price,
                },
                error=None,
            )

        try:
            price = order.price if order.order_type is OrderType.limit else None
            resp = self._ex.create_order(
                order.symbol, order.order_type.value, order.side.value, order.qty, price
            )
            status = str(resp.get("status", "submitted")).lower()
            order_id = resp.get("id")
            filled = float(resp.get("filled") or 0.0)
            ok = status not in {"rejected", "canceled", "cancelled", "failed", "error"}
            return OrderResult(
                ok=ok,
                order_id=str(order_id) if order_id is not None else None,
                status=status,
                filled_qty=filled,
                raw=resp if isinstance(resp, dict) else {"value": resp},
                error=None,
            )
        except Exception as exc:
            return OrderResult(
                ok=False, order_id=None, status="error", filled_qty=0.0, raw={}, error=str(exc)
            )

    def fetch_order(self, venue_order_id: str, symbol: str) -> OrderResult:
        """Re-read one order's current state from the exchange (#135).

        ccxt reports ``filled`` and ``average`` cumulatively for the whole
        order, which is why the caller folds this in as a status snapshot
        rather than as an incremental fill.

        A failure is returned as ``ok=False`` rather than raised, and the
        caller treats that as "state unknown" rather than as a rejection --
        an unreachable exchange says nothing about whether the order is live.

        Args:
            venue_order_id: The exchange's own order id.
            symbol: Trading symbol the order belongs to; ccxt requires it.

        Returns:
            The order's current state, or a failed result if it cannot be read.
        """
        try:
            resp = self._ex.fetch_order(venue_order_id, symbol)
        except Exception as exc:
            return OrderResult(
                ok=False, order_id=venue_order_id, status="error",
                filled_qty=0.0, raw={}, error=str(exc),
            )
        status = str(resp.get("status", "open")).lower()
        return OrderResult(
            ok=True,
            order_id=str(resp.get("id") or venue_order_id),
            status=status,
            filled_qty=float(resp.get("filled") or 0.0),
            raw=resp if isinstance(resp, dict) else {"value": resp},
            error=None,
        )

    def get_position(self, symbol: str) -> Position | None:
        if self._market_type == "futures":
            # Derivatives: read the signed position via fetch_positions (long/short).
            try:
                positions = self._ex.fetch_positions([symbol])
            except Exception:
                return None
            for p in positions:
                if p.get("symbol") != symbol:
                    continue
                raw_contracts = float(p.get("contracts") or 0.0)
                size = abs(raw_contracts)
                if size < 1e-9:
                    return None
                raw_side = str(p.get("side", "")).lower()
                if raw_side in ("long", "buy"):
                    side = PositionSide.long
                elif raw_side in ("short", "sell"):
                    side = PositionSide.short
                else:
                    # Unknown/missing side: fall back to the sign of contracts.
                    side = PositionSide.long if raw_contracts >= 0 else PositionSide.short
                return Position(
                    symbol=symbol, side=side, size=size,
                    entry_price=float(p.get("entryPrice") or 0.0),
                )
            return None

        # Spot: derive the position from the base-asset balance (long/flat only).
        base = symbol.split("/")[0].upper()
        try:
            bal = self._ex.fetch_balance()
        except Exception:
            return None
        entry = bal.get(base) or {}
        size = abs(float(entry.get("total", entry.get("free", 0.0)) or 0.0))
        if not entry:
            size = abs(float(bal.get("total", {}).get(base, 0.0) or 0.0))
        if size < 1e-9:
            return None
        # Spot balances are long/flat.
        return Position(symbol=symbol, side=PositionSide.long, size=size, entry_price=0.0)

    def close_position(self, symbol: str) -> OrderResult:
        pos = self.get_position(symbol)
        if pos is None or pos.side is PositionSide.flat or pos.size < 1e-9:
            return OrderResult(
                ok=True, order_id=None, status="no position", filled_qty=0.0, raw={}, error=None
            )
        # place_order already honors the live/dry-run guard.
        return self.place_order(
            Order(
                symbol=symbol,
                side=Side.sell,
                order_type=OrderType.market,
                qty=pos.size,
                reduce_only=True,
            )
        )

    def health_check(self) -> bool:
        try:
            self._ex.fetch_balance()
            return True
        except Exception:
            return False
