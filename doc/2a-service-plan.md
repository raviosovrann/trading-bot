# 2A — Service (Bot Supervisor + API) Implementation Plan

> **For agentic workers:** Implement task-by-task with strict TDD (write the failing test → run it red → minimal code → run it green → commit). Each task is **one PR** through the protected-main gate: **tests on 3.11/3.12/3.13 + pyright + Bandit + CodeQL must pass**. `main` is protected — always branch, never push to it.

**Goal:** A FastAPI service that runs many trading bots concurrently as async tasks, with pluggable venues/strategies, shared rate-limited market data, notional risk caps, and a REST + WebSocket API for an internal team.

**Tech Stack:** FastAPI, uvicorn, existing engine (`src/tradingbot/`). New deps (add to `requirements.txt`): `fastapi>=0.115,<0.116`, `uvicorn[standard]>=0.30,<0.35`.

---

## Context — what already exists (reuse, don't rebuild)

Read these before starting; the service is a thin async orchestration layer over them:

- `src/tradingbot/models.py` — `Candle`, `Signal`, `Order`, `OrderResult`, `Position`, `Side`, `OrderType`, `PositionSide`, `Action`. Domain types are pydantic `BaseModel`; do not redefine.
- `src/tradingbot/venues/base.py` — `ExecutionVenue` protocol: `place_order(order)->OrderResult`, `close_position(symbol)->OrderResult`, `get_position(symbol)->Position|None`, `health_check()->bool`.
- `src/tradingbot/venues/ccxt.py` (`CcxtVenue`, spot, `from_exchange`) and `venues/tradovate.py` (`TradovateVenue`, futures long/short, `from_credentials`, `contract_multiplier(symbol)->float`). Both take an **injected client** and carry a `live` dry-run guard.
- `src/tradingbot/datafeed.py` — `CandleFeed` protocol, `CcxtCandleFeed` (`from_exchange`, `warmup_candles`, `latest_closed_candle`).
- `src/tradingbot/stream.py` — `StreamingFeed` protocol, `CcxtStreamFeed` (`warmup_candles`, `on_bar(handler)`, `run(*symbols)`, `stop()`), `run_with_reconnect(...)`.
- `src/tradingbot/runtime.py` — `CandleProcessor` (`add_candle`, `evaluate`, `process_candle`), `BotRuntime`, `StreamRuntime`.
- `src/tradingbot/router.py` — `SignalRouter(venue).route(signal)->OrderResult`.
- `src/tradingbot/strategies/` — the plugin registry from **2D**: `available_strategies()`, `build_strategy(name, ctx)`, `StrategyContext`. (2A Task A3 depends on this — build 2D first or in parallel.)

## Standards (match these — the codebase is consistent)

- `from __future__ import annotations` at the top of every module.
- Optional third-party imports are guarded: `try: import x ... except Exception: x = None  # type: ignore[assignment]`.
- **Dependency injection for testability:** every I/O boundary takes an injected client/feed; a `from_*` classmethod builds the real one. Tests pass fakes — **no network, no credentials, ever**.
- Venue-style error handling: catch `Exception` and return a result object (`OrderResult(ok=False, status="error", error=str(exc))`) rather than letting it propagate out of a boundary method.
- `logging.getLogger(__name__)`; never `print`.
- Frozen dataclass for config; pydantic `BaseModel` for API DTOs.
- pyright clean. Tests live under `tests/` and import `from tradingbot...`; shared test doubles live in `tests/doubles.py` (`FakeVenue`, `InMemoryCandleFeed`). `pytest.ini` sets `pythonpath = src tests`.
- New service tests go in `tests/service/` (create `tests/service/__init__.py` is **not** needed — pytest discovers by path).

## ⚠️ CRITICAL integration constraint (do not skip — see Task A5)

`CcxtStreamFeed.run()` calls `asyncio.run(self._watch_loop(...))`. **`asyncio.run()` cannot be called while an event loop is already running** — and FastAPI/uvicorn always run one. If a bot task calls `run()` (or `StreamRuntime.start()`, which calls it), the service will raise `RuntimeError: asyncio.run() cannot be called from a running event loop` (or deadlock). **Task A5 fixes this** by adding an `async def run_async(*symbols)` to `CcxtStreamFeed` and an `async def start_async(...)` to `StreamRuntime`, so bots run **in the service's existing loop**. Every other task assumes this refactor is done.

---

## File Structure

- `src/tradingbot/service/__init__.py`
- `src/tradingbot/service/ratelimit.py` — `RateLimiter`
- `src/tradingbot/service/datahub.py` — `MarketDataHub`
- `src/tradingbot/service/registry.py` — `build_venue`, `VenueRegistry`, `StrategyRegistry`
- `src/tradingbot/service/risk.py` — `RiskGuard`
- `src/tradingbot/service/events.py` — `EventBus`, event dataclasses
- `src/tradingbot/service/supervisor.py` — `BotConfig`, `BotInstance`, `BotSupervisor`
- `src/tradingbot/service/store.py` — file-based config + trade history
- `src/tradingbot/service/dto.py` — pydantic request/response models
- `src/tradingbot/service/api.py` — FastAPI app (REST + WS), auth
- `tests/service/test_*.py`

---

## Task A1 — RateLimiter (token bucket)

**Files:** Create `src/tradingbot/service/ratelimit.py`, `tests/service/test_ratelimit.py`

**Interface (Produces):**
```python
class RateLimiter:
    def __init__(self, rate_per_sec: float, burst: int, *,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None: ...
    async def acquire(self) -> None: ...  # blocks until a token is available
```

**Design:** classic token bucket. `tokens` refills at `rate_per_sec`, capped at `burst`. `acquire()` deducts one, awaiting `sleep()` for the shortfall when empty. Inject `clock`/`sleep` so tests are deterministic (no real waiting).

- [ ] **Step 1 — failing test** (`tests/service/test_ratelimit.py`):
```python
import pytest
from tradingbot.service.ratelimit import RateLimiter

@pytest.mark.asyncio
async def test_bucket_paces_after_burst():
    now = [0.0]; slept = []
    async def fake_sleep(s): slept.append(s); now[0] += s
    rl = RateLimiter(rate_per_sec=2.0, burst=2, clock=lambda: now[0], sleep=fake_sleep)
    await rl.acquire(); await rl.acquire()   # burst of 2, no wait
    await rl.acquire()                        # 3rd must wait ~0.5s (1/rate)
    assert slept and abs(slept[-1] - 0.5) < 1e-6
```
Add `pytest-asyncio` to `requirements.txt` (`pytest-asyncio>=0.23,<0.25`) and to `pytest.ini`: `asyncio_mode = auto` (then the `@pytest.mark.asyncio` is optional but harmless).

- [ ] **Step 2 — run red** → `ModuleNotFoundError`.
- [ ] **Step 3 — implement** the token bucket per the interface.
- [ ] **Step 4 — run green.**
- [ ] **Step 5 — commit** on branch `feat/2a-ratelimit`.

---

## Task A2 — MarketDataHub (shared, deduped, WS-first)

**Files:** Create `src/tradingbot/service/datahub.py`, `tests/service/test_datahub.py`

**Interface:**
```python
class MarketDataHub:
    def __init__(self, *, stream_feed: StreamingFeed, candle_feed: CandleFeed,
                 limiter: RateLimiter, mtf_cache_seconds: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None: ...
    def subscribe(self, symbol: str, timeframe: str, handler: Callable[[Candle], None]) -> None: ...
    def unsubscribe(self, symbol: str, timeframe: str, handler: Callable[[Candle], None]) -> None: ...
    async def warmup(self, symbol: str, timeframe: str, limit: int) -> list[Candle]: ...  # limiter+TTL cached, deduped
    def latest_price(self, symbol: str, timeframe: str) -> float | None: ...  # last close seen (for RiskGuard)
```

**Design:** one entry per `(symbol, timeframe)` holding a fan-out list of handlers and the single underlying `StreamingFeed` subscription. The FIRST subscriber starts the stream (via `run_async` from A5); subsequent identical subscribers just add their handler — **one stream, many bots**. `warmup()` funnels through `limiter.acquire()` and caches results per `(symbol, timeframe)` for `mtf_cache_seconds`, so N bots wanting the same warmup/MTF cause ONE fetch. Track `latest_price` from streamed closes for the RiskGuard.

- [ ] **Step 1 — failing test** (assert deduping):
```python
class _FakeCandleFeed:
    def __init__(self): self.calls = 0
    def warmup_candles(self, s, tf, n): self.calls += 1; return [_c(1)]
    def latest_closed_candle(self, s, tf): return _c(1)

async def test_warmup_deduped_and_cached():
    feed = _FakeCandleFeed()
    hub = MarketDataHub(stream_feed=_FakeStream(), candle_feed=feed,
                        limiter=RateLimiter(1000, 1000), mtf_cache_seconds=60,
                        clock=lambda: 0.0)
    await hub.warmup("BTC/USD", "1h", 10)
    await hub.warmup("BTC/USD", "1h", 10)   # same -> served from cache
    assert feed.calls == 1
```
Provide `_c(ts)` → `Candle(timestamp=ts, open=1, high=1, low=1, close=1, volume=1)` and a minimal `_FakeStream` with `on_bar`, `run_async`, `stop`.

- [ ] Steps 2–5 as standard. Also test: two `subscribe` calls on the same `(symbol, timeframe)` start the stream **once** (assert the fake stream's `run_async` invoked once); `unsubscribe` of the last handler stops it. Commit on `feat/2a-datahub`.

---

## Task A3 — VenueRegistry + StrategyRegistry + config (spot/futures selectable)

**Files:** Create `src/tradingbot/service/registry.py`, `tests/service/test_registry.py`

**Interface:**
```python
def build_venue(venue: str, market_type: str, *, creds: dict, live: bool) -> ExecutionVenue:
    # ("coinbase","spot")  -> CcxtVenue.from_exchange(creds["exchange"] or "coinbase", creds["api_key"], creds["api_secret"], creds.get("api_password"), live=live)
    # ("tradovate","futures") -> TradovateVenue.from_credentials(**creds, live=live)
    # anything else -> ValueError

def available_venues() -> list[dict]:   # [{"venue":"coinbase","market_type":"spot"}, {"venue":"tradovate","market_type":"futures"}]
```
`StrategyRegistry` is a thin pass-through to 2D: `available_strategies()`, `build_strategy(name, ctx)`.

**Note on spot vs futures configurability (the user's request):** this is where it lives — `venue` + `market_type` are per-bot config. `coinbase`→`spot`, `tradovate`→`futures`. Keep the mapping table data-driven so a new venue is one row.

- [ ] Test each valid combo builds the right class (monkeypatch `CcxtVenue.from_exchange` / `TradovateVenue.from_credentials` to return sentinels — **no network**); invalid `venue`/`market_type` raises `ValueError`. Commit on `feat/2a-registry`.

---

## Task A4 — RiskGuard (per-bot + global notional caps)

**Files:** Create `src/tradingbot/service/risk.py`, `tests/service/test_risk.py`

**Interface:**
```python
@dataclass
class GlobalExposure:
    used: float = 0.0            # summed notional across all bots

class RiskGuard:  # implements ExecutionVenue by wrapping one
    def __init__(self, venue: ExecutionVenue, *, per_bot_cap: float, global_cap: float,
                 global_state: GlobalExposure, price_source: Callable[[], float | None],
                 multiplier: float = 1.0) -> None: ...
    def place_order(self, order: Order) -> OrderResult: ...   # blocks if over cap
    # close_position/get_position/health_check delegate straight through
```

**Design:** notional = `order.qty × price × multiplier` (spot `multiplier=1`; futures pass `venue.contract_multiplier(symbol)`). If `order.reduce_only` → always allow (closing reduces risk). Else if `notional > per_bot_cap` OR `global_state.used + notional > global_cap` → return `OrderResult(ok=False, order_id=None, status="risk_blocked", filled_qty=0.0, raw={"notional": notional}, error="notional cap exceeded")` and do **not** call the venue. On a successful open, add to `global_state.used`; on a close, subtract. If `price_source()` returns `None`, block (can't size risk) — fail safe.

- [ ] Tests: within-cap order passes through to a fake venue; over per-bot cap blocked (venue not called); a second bot pushing `global_state.used` over `global_cap` blocked; `reduce_only=True` always passes; `price_source()==None` blocks. Commit on `feat/2a-riskguard`.

---

## Task A5 — Streaming async refactor + EventBus + BotSupervisor

**Files:** Modify `src/tradingbot/stream.py`, `src/tradingbot/runtime.py`; create `src/tradingbot/service/events.py`, `src/tradingbot/service/supervisor.py`, `tests/service/test_supervisor.py`

**A5a — async streaming (the critical fix):**
```python
# stream.py — CcxtStreamFeed
async def run_async(self, *symbols: str) -> None:
    if not symbols: raise ValueError("run_async requires a symbol")
    await self._watch_loop(symbols[0])       # move the existing loop body here

def run(self, *symbols: str) -> None:        # keep sync entry for CLI
    asyncio.run(self.run_async(*symbols))
```
Add `run_async` to the `StreamingFeed` Protocol. In `runtime.py`, add:
```python
# StreamRuntime
async def start_async(self, *, install_signals: bool = False) -> None:
    if install_signals: self._install_signal_handlers()
    self._proc.evaluate()                    # evaluate warmed buffer immediately
    await self._feed.run_async(self._symbol) # runs in the caller's loop; no asyncio.run
```
(Keep `start()` for the CLI. Reconnect supervision for the async path can wrap `run_async` in a try/loop mirroring `run_with_reconnect` — acceptable to add a simple async reconnect loop here.)

**A5b — EventBus + observer** (`service/events.py`):
```python
@dataclass
class DecisionEvent: bot_id: str; symbol: str; ts: int; text: str
@dataclass
class OrderEvent: bot_id: str; action: str; status: str; ok: bool; order_id: str | None

class EventBus:
    def publish(self, event: Any) -> None: ...           # fan-out to subscriber queues
    def subscribe(self) -> "asyncio.Queue[Any]": ...     # each WS client gets a queue
    def unsubscribe(self, q) -> None: ...
```
Give `CandleProcessor.__init__` an optional `on_event: Callable[[str], None] | None` and call it where it currently logs decisions/orders (keep the logging too). The supervisor passes a callback that publishes `DecisionEvent`/`OrderEvent`.

**A5c — BotSupervisor** (`service/supervisor.py`):
```python
@dataclass
class BotConfig:
    id: str; venue: str; market_type: str; strategy: str; symbol: str
    timeframe: str; quantity: float; live: bool
    per_bot_cap: float; global_cap: float; params: dict

class BotInstance:  # holds config, runtime, task, status, last_decision, position, pnl
    ...

class BotSupervisor:
    def __init__(self, *, hub_factory, event_bus: EventBus, global_exposure: GlobalExposure) -> None: ...
    def create(self, cfg: BotConfig) -> BotInstance: ...
    async def start(self, bot_id: str) -> None: ...   # launches runtime as asyncio.Task via start_async
    async def stop(self, bot_id: str) -> None: ...    # graceful cancel; reuse runtime.stop()
    def list(self) -> list[BotInstance]: ...
    def get(self, bot_id: str) -> BotInstance | None: ...
```
`start()` builds venue (A3) → wraps in `RiskGuard` (A4) → builds strategy via `build_strategy` with a `StrategyContext` whose `data_feed` is the `MarketDataHub` → constructs `StreamRuntime` → `asyncio.create_task(runtime.start_async())`. Market data comes from the hub (subscribe), not a per-bot feed.

- [ ] Tests (with fake feed/venue/strategy, `asyncio_mode=auto`): `start` then `stop` transitions status and cancels the task; two bots run concurrently; a fake strategy signal produces an `OrderEvent` on the bus; `start_async` evaluates the warmed buffer once at start. Commit on `feat/2a-supervisor`.

---

## Task A6 — FastAPI API (REST + WS) + auth + store

**Files:** Create `src/tradingbot/service/dto.py`, `src/tradingbot/service/store.py`, `src/tradingbot/service/api.py`, `tests/service/test_api.py`

**DTOs** (`dto.py`, pydantic): `CreateBotRequest` (venue, market_type, strategy, symbol, timeframe, quantity, live, per_bot_cap, params — **no secrets**), `BotView` (id, config-without-secrets, status, position, pnl, last_decision), `PatchBotRequest` (optional live, caps).

**Store** (`store.py`): `save_config(cfg)` / `load_configs()` → JSON file (`data/bots.json`); `append_trade(bot_id, order_event)` → JSONL (`data/trades/<bot_id>.jsonl`); `read_trades(bot_id)`. Venue API keys live in a separate server-only secrets file, **never** serialized into `BotView` or logged.

**API** (`api.py`, FastAPI):
- `POST /bots` (create, dry-run default if `live` omitted), `GET /bots`, `GET /bots/{id}`, `PATCH /bots/{id}`, `POST /bots/{id}/start`, `POST /bots/{id}/stop`, `GET /bots/{id}/trades`, `GET /venues` (→ `available_venues()`), `GET /strategies` (→ `available_strategies()`).
- `WS /ws`: on connect, `q = event_bus.subscribe()`; loop `await q.get()` → send JSON; unsubscribe on disconnect.
- **Auth:** `Depends(require_auth)` — a bearer token checked against seeded users in a config file (`data/users.json`, hashed). No public signup.
- Mount 2B's built SPA later (`app.mount("/", StaticFiles(directory="ui/dist", html=True))`).

- [ ] Tests (FastAPI `TestClient`): unauthenticated request → 401; create→start→`GET /bots/{id}` shows `status="running"`; `PATCH` flips `live`; `GET /venues`/`/strategies` non-empty; **assert no secret field appears in any response body** (create a bot with keys, GET it, check keys absent). Commit on `feat/2a-api`.

## Deferred
- SQLite (only when reporting needs queries). Roles beyond basic auth. Prometheus metrics.
