# Tradovate Venue (2C) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `TradovateVenue` that trades CME crypto futures (long **and** short) behind the existing `ExecutionVenue` protocol, so it drops into the bot exactly like `CcxtVenue`.

**Architecture:** Mirror the `CcxtVenue` pattern — a venue class that maps our domain (`Order`/`Position`) onto an **injected client** (fully unit-testable with a fake, no network), plus a thin real HTTP client built in the final task for demo/live use. A `LIVE` dry-run guard short-circuits orders when not live, identical to `CcxtVenue`.

**Tech Stack:** Python 3.11+, `httpx` (new dep, for the real HTTP client only), pytest.

**Scope:** This plan covers **execution only** — placing/closing orders and reading positions (the `ExecutionVenue` side of Tradovate). Tradovate **market data** (candles) is deliberately out of scope here; it belongs with the shared `MarketDataHub` built in 2A, so a Tradovate candle feed is planned there rather than duplicated per venue. Deliverable of this plan: a fully unit-tested `TradovateVenue` that trades long/short on demo/live.

## Global Constraints

- Must run on Python **3.11, 3.12, 3.13** (CI matrix).
- **pyright** must stay clean; guard the optional `httpx` import with `# type: ignore` like other optional deps.
- Tests use **injected fakes only — no network, no credentials**.
- Follow the existing `src/tradingbot/venues/ccxt.py` patterns (optional-import guard, `from_credentials` classmethod, injected client, `_FLAT_TOL = 1e-9`).
- Every change lands via **PR to protected `main`**; full gate (tests 3.11/3.12/3.13 + pyright + Bandit + CodeQL) must pass.
- Reuse models verbatim: `from ..models import Order, OrderResult, OrderType, Position, PositionSide, Side`.
- Spot venues are long-only; **this venue is long/short** (futures). `Side.buy` opens/adds long, `Side.sell` opens/adds short.

---

## File Structure

- Create `src/tradingbot/venues/tradovate.py` — `TradovateVenue` (ExecutionVenue impl) + `from_credentials` + a `_TradovateClient` HTTP wrapper (last task). One file, mirroring `venues/ccxt.py`.
- Create `tests/test_tradovate_venue.py` — venue tests against a fake client.
- Modify `requirements.txt` — add `httpx` (last task).

Model reference (already exists, do not change):
- `Order(symbol:str, side:Side, order_type:OrderType, qty:float, price:float|None=None, reduce_only:bool=False)`
- `OrderResult(ok:bool, order_id:str|None, status:str, filled_qty:float, raw:dict, error:str|None=None)`
- `Position(symbol:str, side:PositionSide, size:float, entry_price:float)`
- `Side.buy="buy" / Side.sell="sell"`, `OrderType.market="market" / limit="limit"`, `PositionSide.long/short/flat`.

`ExecutionVenue` protocol to satisfy: `place_order(order)->OrderResult`, `close_position(symbol)->OrderResult`, `get_position(symbol)->Position|None`, `health_check()->bool`.

---

### Task 1: Venue skeleton + construction guard

**Files:**
- Create: `src/tradingbot/venues/tradovate.py`
- Test: `tests/test_tradovate_venue.py`

**Interfaces:**
- Produces: `TradovateVenue(client=None, *, account_id:int|None=None, account_spec:str|None=None, live:bool=False)`. The injected `client` must expose: `place_order(account_id, account_spec, action, symbol, qty, order_type, price=None, reduce_only=False)->dict`, `list_positions(account_id)->list[dict]`, `account()->dict`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tradovate_venue.py
import pytest
from tradingbot.venues.tradovate import TradovateVenue


class _FakeClient:
    def __init__(self, place_result=None, positions=None, account_raises=False):
        self.place_result = place_result if place_result is not None else {"orderId": 111}
        self.positions = positions or []
        self.account_raises = account_raises
        self.calls = []

    def place_order(self, account_id, account_spec, action, symbol, qty,
                    order_type, price=None, reduce_only=False):
        self.calls.append((action, symbol, qty, order_type, price, reduce_only))
        return self.place_result

    def list_positions(self, account_id):
        return list(self.positions)

    def account(self):
        if self.account_raises:
            raise RuntimeError("auth failed")
        return {"id": 1, "name": "DEMO123"}


def _venue(client, *, live=True):
    return TradovateVenue(client, account_id=1, account_spec="DEMO123", live=live)


def test_construct_requires_client():
    with pytest.raises(ValueError):
        TradovateVenue(None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tradovate_venue.py -q`
Expected: FAIL — `ModuleNotFoundError`/`ImportError` (module/class not defined).

- [ ] **Step 3: Write minimal implementation**

```python
# src/tradingbot/venues/tradovate.py
from __future__ import annotations

from typing import Any

from ..models import Order, OrderResult, OrderType, Position, PositionSide, Side

_FLAT_TOL = 1e-9


class TradovateVenue:
    """Execution venue for Tradovate CME crypto futures (long + short).

    Mirrors CcxtVenue: domain logic maps onto an injected client so it is fully
    unit-testable; the real HTTP client is built in from_credentials. A LIVE
    dry-run guard short-circuits orders when not live.
    """

    def __init__(
        self,
        client: Any | None = None,
        *,
        account_id: int | None = None,
        account_spec: str | None = None,
        live: bool = False,
    ) -> None:
        if client is None:
            raise ValueError("TradovateVenue requires a client or use from_credentials(...)")
        self._client = client
        self._account_id = account_id
        self._account_spec = account_spec
        self._live = live
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_tradovate_venue.py -q`
Expected: PASS (1 test).

- [ ] **Step 5: Commit**

```bash
git checkout -b feat/tradovate-venue
git add src/tradingbot/venues/tradovate.py tests/test_tradovate_venue.py
git commit -m "feat(tradovate): venue skeleton + construction guard"
```

---

### Task 2: place_order — LIVE dry-run guard

**Files:**
- Modify: `src/tradingbot/venues/tradovate.py`
- Test: `tests/test_tradovate_venue.py`

**Interfaces:**
- Produces: `TradovateVenue.place_order(order:Order)->OrderResult`. When `live=False`, returns a dry-run result and never calls the client.

- [ ] **Step 1: Write the failing test**

```python
from tradingbot.models import Order, OrderType, Side


def test_dry_run_does_not_call_client():
    client = _FakeClient()
    venue = _venue(client, live=False)
    r = venue.place_order(Order(symbol="MBTF6", side=Side.buy, order_type=OrderType.market, qty=1))
    assert r.ok is True
    assert r.status == "dry_run"
    assert client.calls == []  # nothing sent
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tradovate_venue.py::test_dry_run_does_not_call_client -v`
Expected: FAIL — `AttributeError: 'TradovateVenue' object has no attribute 'place_order'`.

- [ ] **Step 3: Write minimal implementation** (add method to the class)

```python
    def place_order(self, order: Order) -> OrderResult:
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
        raise NotImplementedError  # live path added in Task 3
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_tradovate_venue.py::test_dry_run_does_not_call_client -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tradingbot/venues/tradovate.py tests/test_tradovate_venue.py
git commit -m "feat(tradovate): LIVE dry-run guard on place_order"
```

---

### Task 3: place_order — live path (buy=long, sell=short, failure mapping)

**Files:**
- Modify: `src/tradingbot/venues/tradovate.py`
- Test: `tests/test_tradovate_venue.py`

**Interfaces:**
- Consumes: `client.place_order(...)` returns a dict — success has `orderId`; failure has `failureReason`/`failureText`.
- Produces: live `place_order` mapping to `OrderResult`.

- [ ] **Step 1: Write the failing test**

```python
def test_live_market_buy_maps_ok_and_sends_buy():
    client = _FakeClient(place_result={"orderId": 555})
    venue = _venue(client, live=True)
    r = venue.place_order(Order(symbol="MBTF6", side=Side.buy, order_type=OrderType.market, qty=2))
    assert r.ok is True and r.order_id == "555" and r.status == "submitted"
    assert client.calls == [("Buy", "MBTF6", 2, "Market", None, False)]


def test_live_market_sell_sends_sell_for_short():
    client = _FakeClient(place_result={"orderId": 7})
    venue = _venue(client, live=True)
    venue.place_order(Order(symbol="MBTF6", side=Side.sell, order_type=OrderType.market, qty=1))
    assert client.calls[0][0] == "Sell"  # opens/adds a short on futures


def test_live_limit_passes_price():
    client = _FakeClient(place_result={"orderId": 9})
    venue = _venue(client, live=True)
    venue.place_order(Order(symbol="MBTF6", side=Side.buy, order_type=OrderType.limit, qty=1, price=64000.0))
    assert client.calls[0][3] == "Limit" and client.calls[0][4] == 64000.0


def test_live_failure_returns_not_ok():
    client = _FakeClient(place_result={"failureReason": "InsufficientMargin", "failureText": "no funds"})
    venue = _venue(client, live=True)
    r = venue.place_order(Order(symbol="MBTF6", side=Side.buy, order_type=OrderType.market, qty=1))
    assert r.ok is False and r.status == "rejected" and "no funds" in (r.error or "")


def test_live_client_exception_returns_error():
    class _Boom(_FakeClient):
        def place_order(self, *a, **k):
            raise RuntimeError("network down")
    venue = _venue(_Boom(), live=True)
    r = venue.place_order(Order(symbol="MBTF6", side=Side.buy, order_type=OrderType.market, qty=1))
    assert r.ok is False and r.status == "error" and "network down" in (r.error or "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tradovate_venue.py -k live -v`
Expected: FAIL — `NotImplementedError`.

- [ ] **Step 3: Write minimal implementation** (replace the `raise NotImplementedError` line)

```python
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
                    ok=False, order_id=str(order_id) if order_id is not None else None,
                    status="rejected", filled_qty=0.0, raw=resp,
                    error=str(resp.get("failureText") or resp.get("failureReason") or "order rejected"),
                )
            # Tradovate fills arrive asynchronously; placement only confirms acceptance.
            return OrderResult(
                ok=True, order_id=str(order_id), status="submitted",
                filled_qty=0.0, raw=resp, error=None,
            )
        except Exception as exc:
            return OrderResult(
                ok=False, order_id=None, status="error", filled_qty=0.0, raw={}, error=str(exc),
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_tradovate_venue.py -k live -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/tradingbot/venues/tradovate.py tests/test_tradovate_venue.py
git commit -m "feat(tradovate): live place_order with long/short + failure mapping"
```

---

### Task 4: get_position — long/short/flat from netPos

**Files:**
- Modify: `src/tradingbot/venues/tradovate.py`
- Test: `tests/test_tradovate_venue.py`

**Interfaces:**
- Consumes: `client.list_positions(account_id)` returns `list[dict]`, each with `symbol` and signed `netPos` (contracts), optional `netPrice` (avg entry).
- Produces: `get_position(symbol)->Position|None`.

- [ ] **Step 1: Write the failing test**

```python
from tradingbot.models import PositionSide


def test_get_position_long_from_positive_netpos():
    client = _FakeClient(positions=[{"symbol": "MBTF6", "netPos": 3, "netPrice": 64000.0}])
    pos = _venue(client).get_position("MBTF6")
    assert pos is not None and pos.side is PositionSide.long
    assert pos.size == 3 and pos.entry_price == 64000.0


def test_get_position_short_from_negative_netpos():
    client = _FakeClient(positions=[{"symbol": "MBTF6", "netPos": -2}])
    pos = _venue(client).get_position("MBTF6")
    assert pos is not None and pos.side is PositionSide.short and pos.size == 2


def test_get_position_flat_or_absent_returns_none():
    assert _venue(_FakeClient(positions=[])).get_position("MBTF6") is None
    zero = _FakeClient(positions=[{"symbol": "MBTF6", "netPos": 0}])
    assert _venue(zero).get_position("MBTF6") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tradovate_venue.py -k get_position -v`
Expected: FAIL — `AttributeError: ... 'get_position'`.

- [ ] **Step 3: Write minimal implementation**

```python
    def get_position(self, symbol: str) -> Position | None:
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_tradovate_venue.py -k get_position -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/tradingbot/venues/tradovate.py tests/test_tradovate_venue.py
git commit -m "feat(tradovate): get_position long/short/flat from netPos"
```

---

### Task 5: close_position — opposite side, reduce_only

**Files:**
- Modify: `src/tradingbot/venues/tradovate.py`
- Test: `tests/test_tradovate_venue.py`

**Interfaces:**
- Produces: `close_position(symbol)->OrderResult`. Flattens by placing an opposite-side market order for the held size.

- [ ] **Step 1: Write the failing test**

```python
def test_close_long_sells_size_reduce_only():
    client = _FakeClient(positions=[{"symbol": "MBTF6", "netPos": 3}], place_result={"orderId": 1})
    r = _venue(client, live=True).close_position("MBTF6")
    assert r.ok is True
    assert client.calls == [("Sell", "MBTF6", 3, "Market", None, True)]


def test_close_short_buys_size_reduce_only():
    client = _FakeClient(positions=[{"symbol": "MBTF6", "netPos": -2}], place_result={"orderId": 1})
    _venue(client, live=True).close_position("MBTF6")
    assert client.calls[0][0] == "Buy" and client.calls[0][5] is True


def test_close_when_flat_is_noop():
    client = _FakeClient(positions=[])
    r = _venue(client, live=True).close_position("MBTF6")
    assert r.ok is True and r.status == "no position" and client.calls == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tradovate_venue.py -k close -v`
Expected: FAIL — `AttributeError: ... 'close_position'`.

- [ ] **Step 3: Write minimal implementation**

```python
    def close_position(self, symbol: str) -> OrderResult:
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_tradovate_venue.py -k close -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/tradingbot/venues/tradovate.py tests/test_tradovate_venue.py
git commit -m "feat(tradovate): close_position flattens via opposite reduce-only order"
```

---

### Task 6: health_check + contract multiplier

**Files:**
- Modify: `src/tradingbot/venues/tradovate.py`
- Test: `tests/test_tradovate_venue.py`

**Interfaces:**
- Produces: `health_check()->bool`; `contract_multiplier(symbol)->float` (used later by the RiskGuard in 2A to compute notional = contracts × multiplier × price).

- [ ] **Step 1: Write the failing test**

```python
def test_health_check_true_then_false():
    assert _venue(_FakeClient()).health_check() is True
    assert _venue(_FakeClient(account_raises=True)).health_check() is False


def test_contract_multiplier_micro_and_standard():
    v = _venue(_FakeClient())
    assert v.contract_multiplier("MBTF6") == 0.1    # Micro Bitcoin = 0.1 BTC
    assert v.contract_multiplier("METF6") == 0.1    # Micro Ether = 0.1 ETH
    assert v.contract_multiplier("BTCF6") == 5.0    # Bitcoin (full) = 5 BTC
    assert v.contract_multiplier("ETHF6") == 50.0   # Ether (full) = 50 ETH
    assert v.contract_multiplier("UNKNOWN") == 1.0  # safe default
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tradovate_venue.py -k "health or multiplier" -v`
Expected: FAIL — missing attributes.

- [ ] **Step 3: Write minimal implementation** (add near top-of-module a map, and methods on the class)

```python
# module level, near _FLAT_TOL
_CONTRACT_MULTIPLIERS: dict[str, float] = {
    "MBT": 0.1,   # Micro Bitcoin future = 0.1 BTC
    "MET": 0.1,   # Micro Ether future = 0.1 ETH
    "BTC": 5.0,   # Bitcoin future = 5 BTC
    "ETH": 50.0,  # Ether future = 50 ETH
}
```

```python
    def health_check(self) -> bool:
        try:
            self._client.account()
            return True
        except Exception:
            return False

    def contract_multiplier(self, symbol: str) -> float:
        # Tradovate symbols are <product><monthcode><year>, e.g. MBTF6.
        # Match the longest known product prefix; default 1.0.
        for product, mult in sorted(_CONTRACT_MULTIPLIERS.items(), key=lambda kv: -len(kv[0])):
            if symbol.upper().startswith(product):
                return mult
        return 1.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_tradovate_venue.py -k "health or multiplier" -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/tradingbot/venues/tradovate.py tests/test_tradovate_venue.py
git commit -m "feat(tradovate): health_check + contract multiplier map"
```

---

### Task 7: Real HTTP client + from_credentials (demo/live)

> **Verify against Tradovate's API docs (https://api.tradovate.com/) during implementation.** The endpoint paths and JSON field names below reflect Tradovate's documented v1 API but MUST be confirmed on the demo environment. This task's code is exercised manually against the demo env, not in CI (no network in tests).

**Files:**
- Modify: `src/tradingbot/venues/tradovate.py` (add `_TradovateClient` + `TradovateVenue.from_credentials`)
- Modify: `requirements.txt` (add `httpx`)
- Test: `tests/test_tradovate_venue.py` (only the offline guard is tested)

**Interfaces:**
- Consumes: `httpx` (guarded optional import).
- Produces: `TradovateVenue.from_credentials(*, name, password, app_id, app_version, cid, sec, live=False, device_id="")->TradovateVenue`. `_TradovateClient` implements the same 3 methods the fake does.

- [ ] **Step 1: Write the failing test** (offline behaviour only)

```python
def test_from_credentials_requires_httpx(monkeypatch):
    import tradingbot.venues.tradovate as tv
    monkeypatch.setattr(tv, "httpx", None)
    with pytest.raises(RuntimeError):
        tv.TradovateVenue.from_credentials(
            name="u", password="p", app_id="a", app_version="1", cid="1", sec="s",
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tradovate_venue.py::test_from_credentials_requires_httpx -v`
Expected: FAIL — no `from_credentials` / no `httpx` symbol.

- [ ] **Step 3: Write minimal implementation**

Add the guarded import at the top of the module:

```python
try:
    import httpx  # type: ignore
except Exception:  # pragma: no cover - optional third-party install
    httpx = None  # type: ignore[assignment]

_DEMO_BASE = "https://demo.tradovateapi.com/v1"
_LIVE_BASE = "https://live.tradovateapi.com/v1"
```

Add the client and factory:

```python
class _TradovateClient:
    """Thin HTTP wrapper over Tradovate's v1 REST API. Verify endpoints/fields
    against https://api.tradovate.com/ on the demo env before live use."""

    def __init__(self, base_url: str, access_token: str) -> None:
        self._base = base_url
        self._http = httpx.Client(  # type: ignore[union-attr]
            base_url=base_url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15.0,
        )

    def place_order(self, account_id, account_spec, action, symbol, qty,
                    order_type, price=None, reduce_only=False) -> dict:
        payload: dict[str, Any] = {
            "accountId": account_id,
            "accountSpec": account_spec,
            "action": action,            # "Buy" | "Sell"
            "symbol": symbol,
            "orderQty": qty,
            "orderType": order_type,     # "Market" | "Limit"
            "isAutomated": True,
        }
        if price is not None:
            payload["price"] = price
        if reduce_only:
            payload["reduceOnly"] = True
        return self._http.post("/order/placeorder", json=payload).json()

    def list_positions(self, account_id) -> list[dict]:
        rows = self._http.get("/position/list").json()
        # Attach a resolvable symbol per position; Tradovate returns contractId,
        # so map it to a symbol via /contract/item?id=<contractId>.
        out = []
        for r in rows:
            if r.get("accountId") not in (None, account_id):
                continue
            cid = r.get("contractId")
            sym = r.get("symbol")
            if sym is None and cid is not None:
                sym = self._http.get(f"/contract/item?id={cid}").json().get("name")
            out.append({"symbol": sym, "netPos": r.get("netPos", 0), "netPrice": r.get("netPrice")})
        return out

    def account(self) -> dict:
        rows = self._http.get("/account/list").json()
        if not rows:
            raise RuntimeError("no Tradovate account")
        return rows[0]


class _TradovateAuth:
    @staticmethod
    def access_token(base_url: str, creds: dict) -> str:
        resp = httpx.post(f"{base_url}/auth/accesstokenrequest", json=creds, timeout=15.0)  # type: ignore[union-attr]
        data = resp.json()
        token = data.get("accessToken")
        if not token:
            raise RuntimeError(f"Tradovate auth failed: {data.get('errorText') or data}")
        return token
```

Add the factory method on `TradovateVenue`:

```python
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
        return cls(
            client,
            account_id=account.get("id"),
            account_spec=account.get("name"),
            live=live,
        )
```

Add `httpx` to `requirements.txt`:

```
httpx>=0.27,<0.29
```

- [ ] **Step 4: Run test to verify it passes** (and the whole file + pyright)

Run: `.venv/bin/python -m pytest tests/test_tradovate_venue.py -q`
Expected: PASS (all tests).
Run: `pyright src/tradingbot/venues/tradovate.py tests/test_tradovate_venue.py`
Expected: 0 errors (pytest-not-installed noise locally is fine; CI resolves it).

- [ ] **Step 5: Commit, push, open PR**

```bash
git add src/tradingbot/venues/tradovate.py tests/test_tradovate_venue.py requirements.txt
git commit -m "feat(tradovate): real HTTP client + from_credentials (demo/live)"
git push -u origin feat/tradovate-venue
gh pr create --base main --title "feat: Tradovate futures venue (long/short)" \
  --body "2C: TradovateVenue behind the ExecutionVenue protocol — long/short futures, LIVE dry-run guard, injected-client tests. Real HTTP client (httpx) endpoints to be verified on Tradovate demo."
```

- [ ] **Step 6: Wait for CI, address Copilot, merge** (main agent handles per repo workflow).

---

## Manual validation (after merge, on Tradovate demo)

Not part of CI — the operator runs this against a Tradovate **demo** account:
1. Create Tradovate demo API credentials (name/password/appId/cid/sec).
2. `TradovateVenue.from_credentials(..., live=False)` → `health_check()` returns True.
3. `place_order(Order("MBTF6", Side.buy, market, qty=1))` with `live=True` on demo → order appears in the Tradovate demo blotter.
4. `get_position("MBTF6")` reflects the fill; `close_position("MBTF6")` flattens.
