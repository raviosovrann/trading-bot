"""Tradovate execution venue for CME crypto futures (long + short).

Mirrors ``CcxtVenue``: all domain logic maps onto an injected client, so the
venue is fully unit-testable with a fake and no network. The real HTTP client
(``_TradovateClient``) is built in ``from_credentials`` for demo/live use. A
``LIVE`` dry-run guard short-circuits orders when not live, identical to
``CcxtVenue``.
"""

from __future__ import annotations

from typing import Any

from .contracts import ContractMetadataError, ContractSpec
from ..models import Order, OrderResult, OrderType, Position, PositionSide, Side

try:
    import httpx  # type: ignore
except Exception:  # pragma: no cover - optional third-party install
    httpx = None  # type: ignore[assignment]

_FLAT_TOL = 1e-9

# Trailing slash + relative request paths (no leading "/") so httpx preserves
# the "/v1" path segment when joining URLs.
_DEMO_BASE = "https://demo.tradovateapi.com/v1/"
_LIVE_BASE = "https://live.tradovateapi.com/v1/"

# CME crypto-futures contract sizes (units of the underlying per contract),
# prefix-matched against the Tradovate symbol root (e.g. "MBTF6" -> "MBT").
# Both micro and full-size contracts are supported.
_CONTRACT_MULTIPLIERS: dict[str, float] = {
    "MBT": 0.1,   # Micro Bitcoin  = 0.1 BTC
    "MET": 0.1,   # Micro Ether    = 0.1 ETH
    "BTC": 5.0,   # Bitcoin future = 5 BTC
    "ETH": 50.0,  # Ether future   = 50 ETH
}


class TradovateVenue:
    """Execution venue for Tradovate CME futures, long and short.

    Tradovate is natively position-based and symmetric: a short is a genuine
    negative ``netPos`` rather than a margin loan, so ``close_position``
    reverses the sign without the spot-only special cases ``CcxtVenue`` needs.

    Orders confirm acceptance, not execution -- ``place_order`` returns
    ``submitted`` with ``filled_qty=0`` because Tradovate reports fills
    asynchronously. Callers wanting fills must reconcile from the ledger.
    """

    def __init__(
        self,
        client: Any | None = None,
        *,
        account_id: int | None = None,
        account_spec: str | None = None,
        live: bool = False,
    ) -> None:
        """Wrap an injected HTTP client.

        Args:
            client: Client exposing ``place_order``/``list_positions``/
                ``account``. Required -- injecting it is what keeps the venue
                testable without network.
            account_id: Numeric Tradovate account id. Both this and
                ``account_spec`` are needed to place a live order.
            account_spec: Account name Tradovate expects alongside the id.
            live: When False, orders short-circuit to ``dry_run`` and the
                broker is never contacted.

        Raises:
            ValueError: If ``client`` is None.
        """
        if client is None:
            raise ValueError("TradovateVenue requires a client or use from_credentials(...)")
        self._client = client
        self._account_id = account_id
        self._account_spec = account_spec
        self._live = live

    @classmethod
    def from_credentials(
        cls,
        *,
        name: str,
        password: str,
        app_id: str,
        app_version: str,
        cid: str,
        sec: str,
        live: bool = False,
        device_id: str = "",
    ) -> "TradovateVenue":
        """Authenticate and resolve the trading account in one step.

        The account lookup is not optional bookkeeping: Tradovate requires both
        the numeric id and the account name on every order, and discovering
        them here means a misconfigured account fails at construction rather
        than on the first live trade.

        ``live`` selects the API host, so a demo venue physically cannot reach
        the live broker even if the dry-run guard were bypassed.

        Args:
            name: Tradovate username.
            password: Tradovate password.
            app_id: Registered application id.
            app_version: Application version string.
            cid: API client id.
            sec: API client secret.
            live: Selects the live host over demo, and arms real orders.
            device_id: Device identifier Tradovate associates with the session.

        Returns:
            A venue bound to the resolved account.

        Raises:
            RuntimeError: If httpx is missing, auth fails, or the account
                response carries no usable id and name.
        """
        if httpx is None:
            raise RuntimeError("httpx is not installed")
        base = _LIVE_BASE if live else _DEMO_BASE
        creds = {
            "name": name, "password": password, "appId": app_id,
            "appVersion": app_version, "cid": cid, "sec": sec, "deviceId": device_id,
        }
        token = _TradovateAuth.access_token(base, creds)
        client = _TradovateClient(base, token)
        account = client.account()
        account_id = account.get("id")
        account_spec = account.get("name")
        if account_id is None or not account_spec:
            raise RuntimeError(f"Tradovate account response missing id/name: {account}")
        return cls(client, account_id=account_id, account_spec=account_spec, live=live)

    def place_order(self, order: Order) -> OrderResult:
        """Submit an order, or simulate it when not live.

        Never raises: every failure -- transport, rejection, misconfiguration
        -- comes back as an ``OrderResult`` with ``ok=False``. A trading loop
        that dies on one bad order stops managing the positions it already
        holds, which is worse than the rejected order itself.

        A successful result means *accepted*, not filled: ``status`` is
        ``submitted`` and ``filled_qty`` is 0, because Tradovate delivers fills
        asynchronously.

        Args:
            order: Order to place. ``reduce_only`` is passed through so the
                broker enforces that a close cannot flip into a new position.

        Returns:
            ``dry_run`` when not live; ``submitted`` on acceptance;
            ``rejected`` when the broker refuses; ``error`` on transport
            failure or a missing account.
        """
        # LIVE guard: when not live, never touch the broker.
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

        # A live order without an account is a misconfiguration — fail clearly
        # rather than sending a malformed request.
        if self._account_id is None or self._account_spec is None:
            return OrderResult(
                ok=False, order_id=None, status="error", filled_qty=0.0, raw={},
                error="TradovateVenue has no account_id/account_spec (build via from_credentials)",
            )

        try:
            action = "Buy" if order.side is Side.buy else "Sell"
            order_type = "Market" if order.order_type is OrderType.market else "Limit"
            price = order.price if order.order_type is OrderType.limit else None
            resp = self._client.place_order(
                self._account_id,
                self._account_spec,
                action,
                order.symbol,
                order.qty,
                order_type,
                price=price,
                reduce_only=order.reduce_only,
            )
            failure = resp.get("failureReason") or resp.get("failureText")
            order_id = resp.get("orderId")
            if failure or order_id is None:
                return OrderResult(
                    ok=False,
                    order_id=str(order_id) if order_id is not None else None,
                    status="rejected",
                    filled_qty=0.0,
                    raw=resp if isinstance(resp, dict) else {"value": resp},
                    error=str(resp.get("failureText") or resp.get("failureReason") or "order rejected"),
                )
            # Tradovate fills arrive asynchronously; placement only confirms acceptance.
            return OrderResult(
                ok=True,
                order_id=str(order_id),
                status="submitted",
                filled_qty=0.0,
                raw=resp if isinstance(resp, dict) else {"value": resp},
                error=None,
            )
        except Exception as exc:
            return OrderResult(
                ok=False, order_id=None, status="error", filled_qty=0.0, raw={}, error=str(exc),
            )

    def get_position(self, symbol: str) -> Position | None:
        """Return the open position in ``symbol``, or None if flat.

        A netted position below ``_FLAT_TOL`` reads as flat rather than as a
        dust-sized holding, so float noise in ``netPos`` cannot provoke a close
        order for effectively nothing.

        Note a transport failure is also reported as None -- indistinguishable
        from genuinely flat. That is the conservative direction for the one
        caller that matters, ``close_position``, which then does nothing rather
        than sizing a close off an unknown position.

        Args:
            symbol: Tradovate contract symbol to look up.

        Returns:
            The position, or None when flat or unreadable.
        """
        try:
            positions = self._client.list_positions(self._account_id)
        except Exception:
            return None
        for p in positions:
            if str(p.get("symbol")) != symbol:
                continue
            net = float(p.get("netPos", 0) or 0)
            size = abs(net)
            if size < _FLAT_TOL:
                return None
            side = PositionSide.long if net > 0 else PositionSide.short
            entry = float(p.get("netPrice", 0.0) or 0.0)
            return Position(symbol=symbol, side=side, size=size, entry_price=entry)
        return None

    def close_position(self, symbol: str) -> OrderResult:
        """Flatten ``symbol`` with a reduce-only market order.

        Symmetric across directions because Tradovate holds real shorts: the
        close is simply the opposite side, sized to the position. Sent
        reduce-only so a position that shrank between the read and the order
        cannot be flipped into a new one in the other direction (#121).

        Args:
            symbol: Tradovate contract symbol to flatten.

        Returns:
            The closing order's result, or ``no position`` when already flat.
        """
        pos = self.get_position(symbol)
        if pos is None or pos.side is PositionSide.flat or pos.size < _FLAT_TOL:
            return OrderResult(
                ok=True, order_id=None, status="no position", filled_qty=0.0, raw={}, error=None,
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

    def health_check(self) -> bool:
        """Report whether the venue is reachable and the session still valid.

        Uses the account endpoint because it exercises the parts that actually
        break in practice -- network reachability and an unexpired access token
        -- rather than merely that the host answers.

        Returns:
            True if the account call succeeded.
        """
        try:
            self._client.account()
            return True
        except Exception:
            return False

    def contract_spec(self, symbol: str) -> ContractSpec:
        """Resolve ``symbol``'s contract metadata (#124).

        Tradovate exposes no instrument-metadata endpoint through this client,
        so the sizes come from the table above. That is acceptable because CME
        contract sizes are published specifications rather than venue state --
        but it means an unrecognised product is genuinely unknown, and
        ``1.0`` for a futures contract is never a safe guess. It used to be
        the fallback; now it refuses.

        The longest matching prefix wins so ``MBT`` is not resolved as
        ``BTC``: those two differ by a factor of fifty.

        Args:
            symbol: Tradovate contract symbol, e.g. ``MBTF6``.

        Returns:
            The contract's validated spec.

        Raises:
            ContractMetadataError: If the product is not in the table.
        """
        upper = symbol.upper()
        for product, size in sorted(_CONTRACT_MULTIPLIERS.items(), key=lambda kv: -len(kv[0])):
            if upper.startswith(product):
                return ContractSpec(
                    symbol=symbol,
                    contract_size=size,
                    linear=True,
                    quote_currency="USD",
                    settle_currency="USD",
                    tick_size=None,
                    is_derivative=True,
                )
        raise ContractMetadataError(
            f"{symbol}: contract size is not known for this product "
            f"(known: {', '.join(sorted(_CONTRACT_MULTIPLIERS))}); refusing "
            "rather than assuming 1.0, which would misprice exposure"
        )


class _TradovateAuth:
    """Access-token exchange, split out so it can be faked in tests."""

    @staticmethod
    def access_token(base_url: str, creds: dict) -> str:
        """Exchange credentials for a bearer token.

        Tradovate signals auth failure in a 200 response body rather than an
        HTTP status, so a missing ``accessToken`` is checked explicitly --
        ``raise_for_status`` alone would let a failed login through.

        Args:
            base_url: Demo or live API base, with its trailing slash.
            creds: Credential payload for ``auth/accesstokenrequest``.

        Returns:
            The bearer token.

        Raises:
            RuntimeError: If the response carries no token.
            httpx.HTTPError: On transport failure or a non-2xx status.
        """
        # base_url ends with "/"; use a relative path so "/v1" is preserved.
        resp = httpx.post(f"{base_url}auth/accesstokenrequest", json=creds, timeout=15.0)  # type: ignore[union-attr]
        resp.raise_for_status()
        data = resp.json()
        token = data.get("accessToken")
        if not token:
            raise RuntimeError(f"Tradovate auth failed: {data.get('errorText') or data}")
        return token


class _TradovateClient:
    """Thin HTTP wrapper over Tradovate's v1 REST API.

    NOTE: endpoint paths and JSON field names below reflect Tradovate's
    documented v1 API but MUST be verified against https://api.tradovate.com/
    on the demo environment before live use. Request paths are relative (no
    leading "/") so httpx keeps the "/v1" base path.
    """

    def __init__(self, base_url: str, access_token: str) -> None:
        """Open a persistent HTTP session carrying the bearer token.

        Args:
            base_url: Demo or live API base, with its trailing slash so httpx
                preserves the ``/v1`` segment when joining request paths.
            access_token: Bearer token from :class:`_TradovateAuth`.
        """
        self._base = base_url
        self._http = httpx.Client(  # type: ignore[union-attr]
            base_url=base_url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15.0,
        )

    def _get(self, path: str, params: dict | None = None) -> Any:
        resp = self._http.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, json: dict) -> Any:
        resp = self._http.post(path, json=json)
        resp.raise_for_status()
        return resp.json()

    def place_order(self, account_id, account_spec, action, symbol, qty,
                    order_type, price=None, reduce_only=False) -> dict:
        """POST an order to ``order/placeorder``.

        Marked ``isAutomated`` because Tradovate requires algorithmic orders to
        declare themselves; omitting it risks the order being refused.

        Args:
            account_id: Numeric account id.
            account_spec: Account name matching ``account_id``.
            action: ``Buy`` or ``Sell``.
            symbol: Contract symbol.
            qty: Order quantity in contracts.
            order_type: ``Market`` or ``Limit``.
            price: Limit price; omitted for market orders.
            reduce_only: Ask the broker to refuse anything that would open or
                extend a position.

        Returns:
            The decoded order response, including any ``failureReason``.

        Raises:
            httpx.HTTPError: On transport failure or a non-2xx status.
        """
        payload: dict[str, Any] = {
            "accountId": account_id,
            "accountSpec": account_spec,
            "action": action,          # "Buy" | "Sell"
            "symbol": symbol,
            "orderQty": qty,
            "orderType": order_type,   # "Market" | "Limit"
            "isAutomated": True,
        }
        if price is not None:
            payload["price"] = price
        if reduce_only:
            payload["reduceOnly"] = True
        return self._post("order/placeorder", payload)

    def list_positions(self, account_id) -> list[dict]:
        """List open positions, resolving contract ids to symbols.

        ``position/list`` identifies contracts by id rather than name, so rows
        without a symbol cost an extra ``contract/item`` lookup each. Rows are
        filtered to ``account_id`` when it is known; rows carrying no account
        are kept, since the endpoint omits it for single-account sessions.

        Args:
            account_id: Account to filter to, or None to keep every row.

        Returns:
            Dicts of ``symbol``, ``netPos`` and ``netPrice``.

        Raises:
            httpx.HTTPError: On transport failure or a non-2xx status.
        """
        rows = self._get("position/list")
        out: list[dict] = []
        for r in rows:
            # Filter to this account only when we know it; keep rows with no
            # accountId. (When account_id is None we cannot filter, so keep all.)
            if account_id is not None and r.get("accountId") not in (None, account_id):
                continue
            contract_id = r.get("contractId")
            symbol = r.get("symbol")
            if symbol is None and contract_id is not None:
                symbol = self._get("contract/item", params={"id": contract_id}).get("name")
            out.append({"symbol": symbol, "netPos": r.get("netPos", 0), "netPrice": r.get("netPrice")})
        return out

    def account(self) -> dict:
        """Return the session's first trading account.

        Takes the first entry rather than choosing: this client assumes a
        single-account login, which is what the bot is configured for. A
        multi-account session would need the account selected explicitly.

        Returns:
            The account record, including ``id`` and ``name``.

        Raises:
            RuntimeError: If the session has no accounts.
            httpx.HTTPError: On transport failure or a non-2xx status.
        """
        rows = self._get("account/list")
        if not rows:
            raise RuntimeError("no Tradovate account")
        return rows[0]
