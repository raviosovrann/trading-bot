"""CCXT-backed execution venue (spot, LIVE-guard, futures-ready)."""

from __future__ import annotations

from ..models import Order, OrderResult, OrderType, Position, PositionSide, Side
from .ccxt_contracts import ContractCache
from .capabilities import VenueCapabilities as ContractCapabilities
from .contracts import ContractSpec

try:
    import ccxt  # type: ignore
except Exception:  # pragma: no cover
    ccxt = None  # type: ignore[assignment]


class CcxtVenue:
    """Execution venue backed by a ccxt exchange client.

    Spot only for now. Futures support is a future drop-in via
    ``fetch_positions()`` keyed on ``self._market_type``.
    """

    def __init__(
        self,
        exchange=None,
        *,
        live: bool = False,
        market_type: str = "spot",
        capabilities: ContractCapabilities | None = None,
    ):
        if exchange is None:
            raise ValueError("CcxtVenue requires an exchange or use from_exchange(...)")
        self._ex = exchange
        self._live = live
        self._market_type = market_type
        self._contracts: ContractCache | None = None
        self._capabilities = capabilities
        """Declared venue capabilities (#125), used to fail reduce-only closed."""

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
        capabilities: ContractCapabilities | None = None,
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
        return cls(
            klass(config), live=live, market_type=market_type,
            capabilities=capabilities,
        )

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

        params = self._order_params(order)
        if isinstance(params, str):
            return OrderResult(
                ok=False, order_id=None, status="rejected", filled_qty=0.0,
                raw={}, error=params,
            )

        try:
            price = order.price if order.order_type is OrderType.limit else None
            resp = self._ex.create_order(
                order.symbol, order.order_type.value, order.side.value, order.qty,
                price, params,
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

    def _order_params(self, order: Order) -> dict | str:
        """Build the exchange params for ``order``, or return a refusal reason.

        ``Order.reduce_only`` used to be set and then silently dropped: the
        old call passed no params at all, so a "reduce-only" close was sent as
        an ordinary order and could flip an account through zero during a race
        or a stale position read (#121).

        Spot never receives derivative parameters. Exchanges reject unknown
        keys, and spot has no position to reduce -- a sell there disposes of
        inventory (#125).

        Returns:
            The params mapping, or a string explaining why the order must not
            be sent. A reduce-only order on a venue that cannot guarantee the
            semantics fails closed rather than going out unprotected, because
            an unenforced reduce-only is exactly the flip this issue is about.
        """
        if not order.reduce_only:
            return {}
        if self._market_type == "spot":
            return {}
        if self._capabilities is not None and not self._capabilities.supports_reduce_only:
            return (
                f"{self._market_type} market cannot guarantee reduce-only orders, "
                "so this close was not sent; it could otherwise flip the position "
                "through zero"
            )
        return {"reduceOnly": True}

    def contract_spec(self, symbol: str) -> ContractSpec:
        """Resolve ``symbol``'s contract metadata from the exchange (#124).

        Cached per venue instance, since exposure is checked on every order
        and metadata is a network round trip.

        Args:
            symbol: Instrument symbol as ccxt names it.

        Returns:
            The instrument's validated contract spec.

        Raises:
            ContractMetadataError: If the symbol is not listed, or is a
                derivative whose size or linear/inverse convention the
                exchange does not publish. Never falls back to a default.
        """
        if self._contracts is None:
            self._contracts = ContractCache(self._ex)
        return self._contracts.spec(symbol)

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

    def close_position(
        self,
        symbol: str,
        *,
        owned_qty: float | None = None,
        client_order_id: str | None = None,
    ) -> OrderResult:
        """Close ``symbol`` in the direction that actually flattens it.

        A long is closed with a sell and a short with a buy. This always sent
        a sell, so closing a short *doubled* it rather than flattening it
        (#121). Spot is long-only, so a sell remains correct there.

        Args:
            symbol: Trading symbol to flatten.
            client_order_id: Idempotency key for the closing order (#121).
                A close builds its own order, so without this it had no
                identity and could not be recorded in the ledger -- which is
                why exposure release on close is still all-or-nothing.
            owned_qty: Quantity attributable to the calling bot (#128). For
                spot this must be supplied: ``get_position()`` can only see
                the whole account's balance, so closing on that figure would
                sell coins bought by hand or by another bot on the same
                account. ``None`` falls back to the reported position, which
                is correct for derivatives -- there the venue reports a real
                per-account position rather than a shared balance.

        Returns:
            Result of the closing order, or a no-op result when flat.
        """
        pos = self.get_position(symbol)
        if owned_qty is not None:
            size = owned_qty
        elif pos is None or pos.side is PositionSide.flat:
            size = 0.0
        else:
            size = pos.size

        if size < 1e-9:
            return OrderResult(
                ok=True, order_id=None, status="no position", filled_qty=0.0, raw={}, error=None
            )

        # Sell to flatten a long, buy to flatten a short. A missing position
        # can only be spot, which is long-only, so a sell is right there.
        closing_side = (
            Side.buy if pos is not None and pos.side is PositionSide.short else Side.sell
        )
        closing_order = Order(
            symbol=symbol,
            side=closing_side,
            order_type=OrderType.market,
            qty=size,
            reduce_only=True,
            client_order_id=client_order_id,
        )
        # place_order already honors the live/dry-run guard.
        result = self.place_order(closing_order)
        # Report what was actually sent, so the caller can record the close
        # against the same identity the venue saw.
        raw = dict(result.raw) if isinstance(result.raw, dict) else {}
        raw["closing_order"] = closing_order.model_dump(mode="json")
        return result.model_copy(update={"raw": raw})

    def health_check(self) -> bool:
        try:
            self._ex.fetch_balance()
            return True
        except Exception:
            return False
