# Monolith Trading Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A single Python process that pulls BTC-futures market data from Bybit, runs a strategy on each closed bar, and executes buy/sell/close orders on Bybit testnet.

**Architecture:** Monolith, per design V2: `DataFeed → Strategy → Router → ExecutionVenue → Bybit testnet`. Each unit is isolated behind a small interface and unit-tested with fakes; one live integration test hits Bybit testnet. This plan first removes the now-unused webhook layer, then builds the framework with a **placeholder** SMA-crossover strategy. Porting the real Pine algo is a separate follow-on plan (needs the Pine source).

**Tech Stack:** Python 3.11+, Pydantic v2, `pybit` (Bybit v5 unified API), pytest.

## Global Constraints

- Python 3.11+ (`X | None` unions, `tuple[...]`/`list[...]` generics).
- Pydantic v2 API (`model_validate`, `field_validator`, `model_validator`).
- Secrets only via env/`.env`; `.env` gitignored, never committed. The Bybit API secret is NEVER logged.
- All source under `src/tradingbot/`; all tests under `tests/`.
- Venues are accessed only through the `ExecutionVenue` interface; the Router never imports a concrete venue.
- The exchange is the source of truth for positions (no durable local state in this POC).
- No dead code: the webhook layer is removed, not parked.
- Process rule (per user): each code-review finding gets a GitHub issue first, then a PR that `Closes #N`.

---

## File Structure

```
src/tradingbot/
├── config.py            # MODIFY: bybit + strategy settings (no webhook fields)
├── models.py            # MODIFY: trim Signal (drop token/time); add Candle/Side/Order/OrderResult/Position
├── datafeed.py          # NEW: BybitDataFeed — klines → list[Candle]
├── strategy.py          # NEW: Strategy protocol + SmaCrossStrategy (placeholder)
├── router.py            # NEW: Router — Signal → ExecutionVenue call
├── runtime.py           # NEW: Runtime loop (run_once / run_forever)
├── __main__.py          # NEW: entrypoint wiring config→feed→strategy→router→venue
└── venues/
    ├── __init__.py      # NEW
    ├── base.py          # NEW: ExecutionVenue Protocol
    ├── fake.py          # NEW: FakeVenue (test double)
    └── bybit.py         # NEW: BybitTestnetVenue (pybit)
```

**Deleted in Task 1** (unused after the pivot): `src/tradingbot/app.py`,
`src/tradingbot/auth.py`, `src/tradingbot/parser.py`, `tests/test_webhook.py`,
`tests/test_auth.py`, and `BTC-Futures-TradingBot-Design-V1.md`.

---

### Task 1: Cleanup — remove the webhook layer and V1 spec

**Files:**
- Delete: `src/tradingbot/app.py`, `src/tradingbot/auth.py`, `src/tradingbot/parser.py`, `tests/test_webhook.py`, `tests/test_auth.py`, `BTC-Futures-TradingBot-Design-V1.md`
- Modify: `requirements.txt`, `src/tradingbot/models.py`, `tests/test_models.py`, `README.md`
- Commit also: the untracked plan doc `docs/superpowers/plans/2026-07-04-monolith-trading-bot.md`

**Interfaces:**
- Consumes: nothing.
- Produces: a slimmer `Signal` (fields `strategy`, `action`, `symbol`, `order_type`, `price`, `quantity`, `position_side` — no `token`/`time`). `parse_signal`/`SignalParseError` no longer exist.

- [ ] **Step 1: Delete the webhook layer, its tests, and the V1 spec**

```bash
git rm src/tradingbot/app.py src/tradingbot/auth.py src/tradingbot/parser.py \
       tests/test_webhook.py tests/test_auth.py BTC-Futures-TradingBot-Design-V1.md
```

- [ ] **Step 2: Trim `Signal` in `src/tradingbot/models.py`**

Remove the `token: str` and `time: str | None = None` fields from the `Signal`
class. The class must read exactly:
```python
class Signal(BaseModel):
    strategy: str
    action: Action
    symbol: str
    order_type: OrderType = OrderType.market
    price: float | None = None
    quantity: float
    position_side: PositionSide

    @field_validator("quantity")
    @classmethod
    def _quantity_positive(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0:
            raise ValueError("quantity must be a finite number > 0")
        return v

    @model_validator(mode="after")
    def _limit_requires_price(self) -> "Signal":
        if self.order_type is OrderType.limit and self.price is None:
            raise ValueError("price is required for limit orders")
        return self
```
Leave the `Action`, `OrderType`, `PositionSide` enums and the existing imports
(`math`, `pydantic.BaseModel/field_validator/model_validator`) intact.

- [ ] **Step 3: Rewrite `tests/test_models.py` (test `Signal` directly, no parser/token)**

```python
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
```

- [ ] **Step 4: Update `requirements.txt` (drop webhook-only deps, add pybit)**

Replace the file with:
```
pydantic==2.10.4
pytest==8.3.4
pybit==5.8.0
```

- [ ] **Step 5: Remove the "Parked: webhook ingress" section from `README.md`**

Delete the entire `## Parked: webhook ingress` section (heading and body) at the
end of `README.md`. Leave the rest of the README as-is.

- [ ] **Step 6: Run the suite**

Run: `. .venv/bin/activate && pip install -r requirements.txt && pytest -v`
Expected: PASS. Only `tests/test_config.py` (unchanged M0 config tests) and the
rewritten `tests/test_models.py` remain; the deleted webhook/auth tests are gone.

- [ ] **Step 7: Commit (include the plan doc)**

```bash
git add -u
git add docs/superpowers/plans/2026-07-04-monolith-trading-bot.md
git commit -m "chore: remove webhook layer + V1 spec; trim Signal; add pybit"
```
(`git add -u` stages the deletions and the modified tracked files; then add the
new plan doc explicitly. Do NOT `git add .` — `.superpowers/` must stay untracked.)

---

### Task 2: Config for the monolith

**Files:**
- Modify: `src/tradingbot/config.py`
- Modify: `tests/test_config.py`
- Modify: `.env.example`

**Interfaces:**
- Consumes: nothing.
- Produces: `Config(bybit_api_key: str, bybit_api_secret: str, bybit_testnet: bool, symbol: str, timeframe: str, order_qty: float)`; `load_config(env=None) -> Config`; `require_bybit_credentials(cfg) -> None` raising `ConfigError` when key/secret empty. No webhook fields.

- [ ] **Step 1: Write the failing tests**

Replace `tests/test_config.py` with:
```python
import pytest
from tradingbot.config import load_config, Config, ConfigError, require_bybit_credentials


def test_defaults():
    cfg = load_config({})
    assert isinstance(cfg, Config)
    assert cfg.symbol == "BTCUSDT"
    assert cfg.timeframe == "5"
    assert cfg.order_qty == 0.001
    assert cfg.bybit_testnet is True
    assert cfg.bybit_api_key == ""


def test_reads_settings():
    cfg = load_config({
        "BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s", "BYBIT_TESTNET": "false",
        "SYMBOL": "ETHUSDT", "TIMEFRAME": "15", "ORDER_QTY": "0.01",
    })
    assert cfg.bybit_api_key == "k" and cfg.bybit_api_secret == "s"
    assert cfg.bybit_testnet is False
    assert cfg.symbol == "ETHUSDT" and cfg.timeframe == "15" and cfg.order_qty == 0.01


def test_require_credentials_raises_when_missing():
    with pytest.raises(ConfigError):
        require_bybit_credentials(load_config({}))


def test_require_credentials_ok_when_present():
    require_bybit_credentials(load_config({"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"}))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL (new fields / `require_bybit_credentials` don't exist yet).

- [ ] **Step 3: Rewrite `src/tradingbot/config.py`**

```python
import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    bybit_api_key: str
    bybit_api_secret: str
    bybit_testnet: bool
    symbol: str
    timeframe: str
    order_qty: float


def _as_bool(value: str, default: bool) -> bool:
    if value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    return Config(
        bybit_api_key=(env.get("BYBIT_API_KEY") or "").strip(),
        bybit_api_secret=(env.get("BYBIT_API_SECRET") or "").strip(),
        bybit_testnet=_as_bool(env.get("BYBIT_TESTNET", ""), default=True),
        symbol=env.get("SYMBOL", "BTCUSDT"),
        timeframe=env.get("TIMEFRAME", "5"),
        order_qty=float(env.get("ORDER_QTY", "0.001")),
    )


def require_bybit_credentials(cfg: Config) -> None:
    if not cfg.bybit_api_key or not cfg.bybit_api_secret:
        raise ConfigError("Missing BYBIT_API_KEY / BYBIT_API_SECRET")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Replace `.env.example`**

```
# Copy to .env and fill in. .env is gitignored — never commit real values.
# Bybit testnet API credentials (testnet.bybit.com → API Management)
BYBIT_API_KEY=
BYBIT_API_SECRET=
BYBIT_TESTNET=true
SYMBOL=BTCUSDT
TIMEFRAME=5
ORDER_QTY=0.001
```

- [ ] **Step 6: Commit**

```bash
git add src/tradingbot/config.py tests/test_config.py .env.example
git commit -m "feat: monolith config (bybit + strategy settings)"
```

---

### Task 3: Trade models (Candle, Side, Order, OrderResult, Position)

**Files:**
- Modify: `src/tradingbot/models.py`
- Test: `tests/test_trade_models.py`

**Interfaces:**
- Consumes: existing `OrderType` enum.
- Produces: `Candle(timestamp:int, open, high, low, close, volume: float)`; `Side(str, Enum)` `buy`/`sell`; `Order(symbol:str, side:Side, order_type:OrderType, qty:float, price:float|None=None, reduce_only:bool=False)`; `OrderResult(ok:bool, order_id:str|None, status:str, filled_qty:float, raw:dict, error:str|None=None)`; `Position(symbol:str, side:str, size:float, entry_price:float)`.

- [ ] **Step 1: Write the failing test**

`tests/test_trade_models.py`:
```python
from tradingbot.models import Candle, Side, Order, OrderResult, Position, OrderType


def test_candle_fields():
    c = Candle(timestamp=1000, open=1.0, high=2.0, low=0.5, close=1.5, volume=10.0)
    assert c.close == 1.5 and c.high == 2.0


def test_order_defaults():
    o = Order(symbol="BTCUSDT", side=Side.buy, order_type=OrderType.market, qty=0.01)
    assert o.reduce_only is False and o.price is None and o.side is Side.buy


def test_order_result_and_position():
    r = OrderResult(ok=True, order_id="1", status="ok", filled_qty=0.01, raw={})
    assert r.ok and r.error is None
    p = Position(symbol="BTCUSDT", side="long", size=0.01, entry_price=60000.0)
    assert p.side == "long"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_trade_models.py -v`
Expected: FAIL (`ImportError` for `Candle`).

- [ ] **Step 3: Append to `src/tradingbot/models.py`**

```python
class Side(str, Enum):
    buy = "buy"
    sell = "sell"


class Candle(BaseModel):
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class Order(BaseModel):
    symbol: str
    side: Side
    order_type: OrderType
    qty: float
    price: float | None = None
    reduce_only: bool = False


class OrderResult(BaseModel):
    ok: bool
    order_id: str | None
    status: str
    filled_qty: float
    raw: dict
    error: str | None = None


class Position(BaseModel):
    symbol: str
    side: str  # "long" | "short" | "flat"
    size: float
    entry_price: float
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_trade_models.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/tradingbot/models.py tests/test_trade_models.py
git commit -m "feat: add Candle/Side/Order/OrderResult/Position models"
```

---

### Task 4: ExecutionVenue interface + FakeVenue

**Files:**
- Create: `src/tradingbot/venues/__init__.py` (empty)
- Create: `src/tradingbot/venues/base.py`
- Create: `src/tradingbot/venues/fake.py`
- Test: `tests/test_fake_venue.py`

**Interfaces:**
- Consumes: `Order`, `OrderResult`, `Position`, `Side`, `OrderType`.
- Produces: `ExecutionVenue` Protocol (`place_order`, `close_position`, `get_position`, `health_check`); `FakeVenue` in-memory impl tracking one net position per symbol and recording `.orders`.

- [ ] **Step 1: Write the failing test**

`tests/test_fake_venue.py`:
```python
from tradingbot.venues.fake import FakeVenue
from tradingbot.models import Order, Side, OrderType


def test_buy_opens_long_then_close_flattens():
    v = FakeVenue()
    r = v.place_order(Order(symbol="BTCUSDT", side=Side.buy, order_type=OrderType.market, qty=0.01))
    assert r.ok
    assert v.get_position("BTCUSDT").side == "long"
    assert v.close_position("BTCUSDT").ok
    assert v.get_position("BTCUSDT").side == "flat"


def test_records_orders_and_healthcheck():
    v = FakeVenue()
    v.place_order(Order(symbol="BTCUSDT", side=Side.sell, order_type=OrderType.market, qty=0.02))
    assert v.health_check() is True
    assert len(v.orders) == 1
    assert v.get_position("BTCUSDT").side == "short"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_fake_venue.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Write the implementations**

`src/tradingbot/venues/__init__.py`: empty file.

`src/tradingbot/venues/base.py`:
```python
from typing import Protocol

from ..models import Order, OrderResult, Position


class ExecutionVenue(Protocol):
    def place_order(self, order: Order) -> OrderResult: ...
    def close_position(self, symbol: str) -> OrderResult: ...
    def get_position(self, symbol: str) -> Position | None: ...
    def health_check(self) -> bool: ...
```

`src/tradingbot/venues/fake.py`:
```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_fake_venue.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/tradingbot/venues/__init__.py src/tradingbot/venues/base.py src/tradingbot/venues/fake.py tests/test_fake_venue.py
git commit -m "feat: ExecutionVenue interface and in-memory FakeVenue"
```

---

### Task 5: BybitTestnetVenue

**Files:**
- Create: `src/tradingbot/venues/bybit.py`
- Test: `tests/test_bybit_venue.py`

**Interfaces:**
- Consumes: `Order`, `OrderResult`, `Position`, `Side`, `OrderType`; `pybit.unified_trading.HTTP`.
- Produces: `BybitTestnetVenue(client, category="linear")` + `.from_credentials(api_key, api_secret, testnet=True, category="linear")`; implements `ExecutionVenue`. Never logs the secret.

- [ ] **Step 1: Write the failing test (fake pybit client)**

`tests/test_bybit_venue.py`:
```python
import pytest
from tradingbot.venues.bybit import BybitTestnetVenue
from tradingbot.models import Order, Side, OrderType


class FakeHTTP:
    def __init__(self):
        self.calls = []

    def place_order(self, **kw):
        self.calls.append(("place_order", kw))
        return {"retCode": 0, "retMsg": "OK", "result": {"orderId": "abc"}}

    def get_positions(self, **kw):
        return {"retCode": 0, "result": {"list": [
            {"side": "Buy", "size": "0.01", "avgPrice": "60000"}]}}

    def get_wallet_balance(self, **kw):
        return {"retCode": 0, "result": {}}


def test_place_order_maps_to_pybit():
    http = FakeHTTP()
    r = BybitTestnetVenue(http).place_order(
        Order(symbol="BTCUSDT", side=Side.buy, order_type=OrderType.market, qty=0.01))
    assert r.ok and r.order_id == "abc"
    _, kw = http.calls[0]
    assert kw["symbol"] == "BTCUSDT" and kw["side"] == "Buy"
    assert kw["orderType"] == "Market" and kw["qty"] == "0.01"


def test_get_position_parses_long():
    pos = BybitTestnetVenue(FakeHTTP()).get_position("BTCUSDT")
    assert pos.side == "long" and pos.size == 0.01 and pos.entry_price == 60000.0


def test_health_check_true_on_retcode_zero():
    assert BybitTestnetVenue(FakeHTTP()).health_check() is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_bybit_venue.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Write `src/tradingbot/venues/bybit.py`**

```python
from ..models import Order, OrderResult, OrderType, Position, Side


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
            return Position(symbol=symbol, side="flat", size=0.0, entry_price=0.0)
        side = "long" if row.get("side") == "Buy" else "short"
        return Position(symbol=symbol, side=side, size=size,
                        entry_price=float(row.get("avgPrice") or 0))

    def close_position(self, symbol: str) -> OrderResult:
        pos = self.get_position(symbol)
        if pos is None or pos.side == "flat" or pos.size == 0:
            return OrderResult(ok=True, order_id=None, status="no position",
                               filled_qty=0.0, raw={})
        close_side = Side.sell if pos.side == "long" else Side.buy
        return self.place_order(Order(symbol=symbol, side=close_side,
                                      order_type=OrderType.market, qty=pos.size,
                                      reduce_only=True))

    def health_check(self) -> bool:
        try:
            resp = self._client.get_wallet_balance(accountType="UNIFIED")
            return resp.get("retCode") == 0
        except Exception:
            return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_bybit_venue.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Add a live integration test, skipped without creds**

Append to `tests/test_bybit_venue.py`:
```python
import os


@pytest.mark.skipif(
    not (os.getenv("BYBIT_API_KEY") and os.getenv("BYBIT_API_SECRET")),
    reason="no Bybit testnet credentials in env",
)
def test_live_testnet_health_check():
    v = BybitTestnetVenue.from_credentials(
        os.environ["BYBIT_API_KEY"], os.environ["BYBIT_API_SECRET"], testnet=True)
    assert v.health_check() is True
```

- [ ] **Step 6: Run the file (integration test skips locally)**

Run: `pytest tests/test_bybit_venue.py -v`
Expected: PASS (3 passed, 1 skipped).

- [ ] **Step 7: Commit**

```bash
git add src/tradingbot/venues/bybit.py tests/test_bybit_venue.py
git commit -m "feat: BybitTestnetVenue via pybit with injectable client"
```

---

### Task 6: DataFeed (Bybit klines → Candles)

**Files:**
- Create: `src/tradingbot/datafeed.py`
- Test: `tests/test_datafeed.py`

**Interfaces:**
- Consumes: `Candle`; a client with `get_kline(...)`.
- Produces: `BybitDataFeed(client, symbol, timeframe, category="linear")`; `get_candles(limit=200) -> list[Candle]` oldest-first.

- [ ] **Step 1: Write the failing test**

`tests/test_datafeed.py`:
```python
from tradingbot.datafeed import BybitDataFeed


class FakeHTTP:
    def get_kline(self, **kw):
        self.kw = kw
        return {"retCode": 0, "result": {"list": [
            ["2000", "3", "4", "2", "3.5", "10", "0"],
            ["1000", "1", "2", "0.5", "1.5", "5", "0"],
        ]}}


def test_get_candles_oldest_first():
    candles = BybitDataFeed(FakeHTTP(), "BTCUSDT", "5").get_candles(limit=2)
    assert [c.timestamp for c in candles] == [1000, 2000]
    assert candles[0].close == 1.5 and candles[1].close == 3.5


def test_passes_symbol_and_interval():
    http = FakeHTTP()
    BybitDataFeed(http, "ETHUSDT", "15").get_candles(limit=2)
    assert http.kw["symbol"] == "ETHUSDT" and http.kw["interval"] == "15"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_datafeed.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Write `src/tradingbot/datafeed.py`**

```python
from .models import Candle


class BybitDataFeed:
    def __init__(self, client, symbol: str, timeframe: str, category: str = "linear") -> None:
        self._client = client
        self._symbol = symbol
        self._timeframe = timeframe
        self._category = category

    def get_candles(self, limit: int = 200) -> list[Candle]:
        resp = self._client.get_kline(
            category=self._category, symbol=self._symbol,
            interval=self._timeframe, limit=limit,
        )
        rows = (resp.get("result") or {}).get("list") or []
        return [
            Candle(timestamp=int(r[0]), open=float(r[1]), high=float(r[2]),
                   low=float(r[3]), close=float(r[4]), volume=float(r[5]))
            for r in reversed(rows)
        ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_datafeed.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/tradingbot/datafeed.py tests/test_datafeed.py
git commit -m "feat: BybitDataFeed maps klines to oldest-first Candles"
```

---

### Task 7: Strategy interface + SMA-crossover placeholder

**Files:**
- Create: `src/tradingbot/strategy.py`
- Test: `tests/test_strategy.py`

**Interfaces:**
- Consumes: `Candle`, `Signal`, `Action`, `OrderType`, `PositionSide`.
- Produces: `Strategy` Protocol (`on_bar(candles) -> Signal | None`); `SmaCrossStrategy(fast, slow, symbol, qty)` emitting a `Signal` on SMA cross (buy on up-cross, sell on down-cross), else `None`.

- [ ] **Step 1: Write the failing test**

`tests/test_strategy.py`:
```python
from tradingbot.strategy import SmaCrossStrategy
from tradingbot.models import Candle, Action


def _candles(closes):
    return [Candle(timestamp=i, open=c, high=c, low=c, close=c, volume=1.0)
            for i, c in enumerate(closes)]


def test_upward_cross_emits_buy():
    strat = SmaCrossStrategy(fast=2, slow=3, symbol="BTCUSDT", qty=0.01)
    sig = strat.on_bar(_candles([1, 1, 1, 5]))
    assert sig is not None and sig.action is Action.buy
    assert sig.symbol == "BTCUSDT" and sig.quantity == 0.01


def test_no_signal_without_enough_data():
    strat = SmaCrossStrategy(fast=2, slow=3, symbol="BTCUSDT", qty=0.01)
    assert strat.on_bar(_candles([1, 2])) is None


def test_no_repeat_signal_when_no_new_cross():
    strat = SmaCrossStrategy(fast=2, slow=3, symbol="BTCUSDT", qty=0.01)
    strat.on_bar(_candles([1, 1, 1, 5]))
    assert strat.on_bar(_candles([1, 1, 1, 5, 6])) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_strategy.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Write `src/tradingbot/strategy.py`**

```python
from typing import Protocol

from .models import Action, Candle, OrderType, PositionSide, Signal


class Strategy(Protocol):
    def on_bar(self, candles: list[Candle]) -> Signal | None: ...


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


class SmaCrossStrategy:
    """Placeholder algo: fast/slow SMA crossover on closes. Emits on cross only."""

    def __init__(self, fast: int, slow: int, symbol: str, qty: float) -> None:
        self._fast = fast
        self._slow = slow
        self._symbol = symbol
        self._qty = qty
        self._last_state: str | None = None

    def on_bar(self, candles: list[Candle]) -> Signal | None:
        closes = [c.close for c in candles]
        fast = _sma(closes, self._fast)
        slow = _sma(closes, self._slow)
        if fast is None or slow is None:
            return None
        state = "above" if fast > slow else "below"
        if self._last_state is None:
            self._last_state = state
            return None
        if state == self._last_state:
            return None
        self._last_state = state
        if state == "above":
            return Signal(strategy="sma-cross", action=Action.buy, symbol=self._symbol,
                          order_type=OrderType.market, quantity=self._qty,
                          position_side=PositionSide.long)
        return Signal(strategy="sma-cross", action=Action.sell, symbol=self._symbol,
                      order_type=OrderType.market, quantity=self._qty,
                      position_side=PositionSide.short)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_strategy.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/tradingbot/strategy.py tests/test_strategy.py
git commit -m "feat: Strategy interface and SMA-crossover placeholder"
```

---

### Task 8: Router (Signal → venue)

**Files:**
- Create: `src/tradingbot/router.py`
- Test: `tests/test_router.py`

**Interfaces:**
- Consumes: `ExecutionVenue` (structural), `Signal`, `Action`, `Order`, `OrderResult`, `Side`, `OrderType`.
- Produces: `Router(venue)` with `handle(signal) -> OrderResult`: buy → place Buy market; sell → place Sell market; close → `venue.close_position`.

- [ ] **Step 1: Write the failing test**

`tests/test_router.py`:
```python
from tradingbot.router import Router
from tradingbot.venues.fake import FakeVenue
from tradingbot.models import Signal, Action, OrderType, PositionSide, Side


def _sig(action):
    return Signal(strategy="s", action=action, symbol="BTCUSDT",
                  order_type=OrderType.market, quantity=0.01,
                  position_side=PositionSide.long)


def test_buy_places_buy_order():
    v = FakeVenue()
    Router(v).handle(_sig(Action.buy))
    assert v.orders[0].side is Side.buy and v.orders[0].qty == 0.01


def test_sell_places_sell_order():
    v = FakeVenue()
    Router(v).handle(_sig(Action.sell))
    assert v.orders[0].side is Side.sell


def test_close_flattens_position():
    v = FakeVenue()
    r = Router(v)
    r.handle(_sig(Action.buy))
    r.handle(_sig(Action.close))
    assert v.get_position("BTCUSDT").side == "flat"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_router.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Write `src/tradingbot/router.py`**

```python
import logging

from .models import Action, Order, OrderResult, OrderType, Side, Signal
from .venues.base import ExecutionVenue

logger = logging.getLogger("tradingbot")


class Router:
    def __init__(self, venue: ExecutionVenue) -> None:
        self._venue = venue

    def handle(self, signal: Signal) -> OrderResult:
        if signal.action is Action.close:
            logger.info("Routing CLOSE %s", signal.symbol)
            return self._venue.close_position(signal.symbol)
        side = Side.buy if signal.action is Action.buy else Side.sell
        order = Order(symbol=signal.symbol, side=side,
                      order_type=OrderType.market, qty=signal.quantity)
        logger.info("Routing %s %s qty=%s", side.value, signal.symbol, signal.quantity)
        return self._venue.place_order(order)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_router.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/tradingbot/router.py tests/test_router.py
git commit -m "feat: Router maps Signal to venue calls"
```

---

### Task 9: Runtime loop + entrypoint

**Files:**
- Create: `src/tradingbot/runtime.py`
- Create: `src/tradingbot/__main__.py`
- Test: `tests/test_runtime.py`

**Interfaces:**
- Consumes: a feed with `get_candles(limit)`, a `Strategy`, a `Router`, `Config`, `require_bybit_credentials`.
- Produces: `Runtime(feed, strategy, router, candle_limit=200)` with `run_once() -> OrderResult | None` and `run_forever(poll_seconds, sleep=time.sleep)`; `__main__.py` wiring real components from `Config`.

- [ ] **Step 1: Write the failing test**

`tests/test_runtime.py`:
```python
from tradingbot.runtime import Runtime
from tradingbot.router import Router
from tradingbot.venues.fake import FakeVenue
from tradingbot.strategy import SmaCrossStrategy
from tradingbot.models import Candle


class CannedFeed:
    def __init__(self, closes):
        self._closes = closes

    def get_candles(self, limit=200):
        return [Candle(timestamp=i, open=c, high=c, low=c, close=c, volume=1.0)
                for i, c in enumerate(self._closes)]


def test_run_once_routes_signal_to_venue():
    venue = FakeVenue()
    rt = Runtime(CannedFeed([1, 1, 1, 5]), SmaCrossStrategy(2, 3, "BTCUSDT", 0.01), Router(venue))
    result = rt.run_once()
    assert result is not None and result.ok
    assert venue.get_position("BTCUSDT").side == "long"


def test_run_once_returns_none_without_signal():
    venue = FakeVenue()
    rt = Runtime(CannedFeed([1, 2]), SmaCrossStrategy(2, 3, "BTCUSDT", 0.01), Router(venue))
    assert rt.run_once() is None
    assert venue.orders == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_runtime.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Write `src/tradingbot/runtime.py`**

```python
import logging
import time

from .models import OrderResult
from .router import Router
from .strategy import Strategy

logger = logging.getLogger("tradingbot")


class Runtime:
    def __init__(self, feed, strategy: Strategy, router: Router, candle_limit: int = 200) -> None:
        self._feed = feed
        self._strategy = strategy
        self._router = router
        self._candle_limit = candle_limit

    def run_once(self) -> OrderResult | None:
        candles = self._feed.get_candles(limit=self._candle_limit)
        signal = self._strategy.on_bar(candles)
        if signal is None:
            return None
        logger.info("Signal: action=%s symbol=%s qty=%s",
                    signal.action.value, signal.symbol, signal.quantity)
        return self._router.handle(signal)

    def run_forever(self, poll_seconds: float, sleep=time.sleep) -> None:  # pragma: no cover
        logger.info("Runtime loop starting (poll=%ss)", poll_seconds)
        while True:
            try:
                self.run_once()
            except Exception as exc:
                logger.warning("run_once error: %s", exc)
            sleep(poll_seconds)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_runtime.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Write `src/tradingbot/__main__.py`**

```python
import logging

from pybit.unified_trading import HTTP

from .config import load_config, require_bybit_credentials
from .datafeed import BybitDataFeed
from .router import Router
from .runtime import Runtime
from .strategy import SmaCrossStrategy
from .venues.bybit import BybitTestnetVenue

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> None:
    cfg = load_config()
    require_bybit_credentials(cfg)
    client = HTTP(testnet=cfg.bybit_testnet,
                  api_key=cfg.bybit_api_key, api_secret=cfg.bybit_api_secret)
    venue = BybitTestnetVenue(client)
    if not venue.health_check():
        raise SystemExit("Bybit health check failed — check credentials / network")
    feed = BybitDataFeed(client, cfg.symbol, cfg.timeframe)
    strategy = SmaCrossStrategy(fast=9, slow=21, symbol=cfg.symbol, qty=cfg.order_qty)
    Runtime(feed, strategy, Router(venue)).run_forever(poll_seconds=15)


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run the full suite**

Run: `pytest -v`
Expected: PASS (all tests; the live Bybit test skipped without creds).

- [ ] **Step 7: Commit**

```bash
git add src/tradingbot/runtime.py src/tradingbot/__main__.py tests/test_runtime.py
git commit -m "feat: runtime loop and entrypoint wiring data->strategy->router->venue"
```

---

## Follow-on (separate plan, needs Pine source)

Porting the real Pine algo into a `Strategy` implementation replaces
`SmaCrossStrategy` in `__main__.py`, validated against TradingView's free strategy
tester. Running the bot live (`python -m tradingbot`) needs Bybit testnet
credentials in `.env`.

---

## Self-Review

**Spec coverage (V2):** cleanup of unused webhook + V1 spec → Task 1. Config
(no webhook) → Task 2. Trade models → Task 3. ExecutionVenue + Bybit → Tasks 4–5.
DataFeed → Task 6. Strategy interface + placeholder → Task 7. Router → Task 8.
Runtime loop + entrypoint → Task 9. Real Pine port → explicit follow-on.

**Placeholder scan:** No TBD/TODO. The SMA strategy is a deliberate, functional
placeholder (spec calls for it until the Pine source arrives). `run_forever` is
`# pragma: no cover` (infinite loop); `run_once` is fully tested.

**Type consistency:** `Signal(strategy, action, symbol, order_type, price,
quantity, position_side)` (post-trim), `Order(symbol, side:Side, order_type,
qty, price, reduce_only)`, `OrderResult(ok, order_id, status, filled_qty, raw,
error)`, `Position(symbol, side, size, entry_price)`, `ExecutionVenue` methods,
`get_candles(limit)`, `on_bar(candles)`, `Router.handle`, `Runtime.run_once` are
consistent across Tasks 1–9. `Signal` no longer has `token`/`time`; no task
references them.
