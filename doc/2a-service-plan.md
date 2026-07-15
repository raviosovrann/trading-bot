# 2A — Service (Bot Supervisor + API) Implementation Plan

> **For agentic workers:** Implement task-by-task with TDD (failing test → minimal code → pass → commit). Each task is one PR through the protected-main gate (tests 3.11/3.12/3.13 + pyright + Bandit + CodeQL). Follow existing patterns (`venues/ccxt.py`, `venues/tradovate.py`, injected fakes, no network in tests).

**Goal:** A FastAPI service that runs many bots concurrently as async tasks, with pluggable venues/strategies, shared rate-limited market data, notional risk caps, and a REST + WebSocket API.

**Tech Stack:** FastAPI, uvicorn, existing engine. New deps: `fastapi`, `uvicorn[standard]`. Reuse `runtime.py`, `router.py`, venues, `strategies/` (from 2D).

## Global Constraints
- Python 3.11/3.12/3.13; pyright clean; TDD; injected fakes, no network in tests.
- File-based persistence only (JSON config, JSONL trades) — no DB.
- Every change via PR to protected `main`.

## File Structure
- `src/tradingbot/service/registry.py` — `VenueRegistry`, `StrategyRegistry`
- `src/tradingbot/service/ratelimit.py` — `RateLimiter` (token bucket)
- `src/tradingbot/service/datahub.py` — `MarketDataHub` (per venue)
- `src/tradingbot/service/risk.py` — `RiskGuard`
- `src/tradingbot/service/supervisor.py` — `BotInstance`, `BotSupervisor`, `EventBus`
- `src/tradingbot/service/store.py` — file-based config + trade history
- `src/tradingbot/service/api.py` — FastAPI app (REST + WS), auth
- `src/tradingbot/service/dto.py` — pydantic DTOs (`BotConfig`, `BotView`, `Decision`, `OrderEvent`)
- `tests/service/test_*.py`

## Key integration gotcha (read first)
`CcxtStreamFeed.run()` calls `asyncio.run(...)`, which **cannot nest** inside FastAPI's running loop. The supervisor must run the feed's watch loop as an **awaitable task in the existing loop**, not via `StreamRuntime.start()`. **Task A5 refactors** the streaming path to expose an `async def run_async(*symbols)` on `StreamingFeed`/`CcxtStreamFeed` (move the body of `_watch_loop` behind it; keep sync `run()` as `asyncio.run(run_async(...))` for CLI back-compat).

## Tasks

### Task A1 — RateLimiter (token bucket)
- **Files:** `service/ratelimit.py`, `tests/service/test_ratelimit.py`
- **Deliverable:** `RateLimiter(rate_per_sec: float, burst: int)` with `async def acquire()` that paces calls; injectable clock+sleep for deterministic tests.
- **Tests:** N rapid `acquire()` calls take ≥ expected time with a fake clock; burst allowance respected.

### Task A2 — MarketDataHub (shared, deduped, WS-first)
- **Files:** `service/datahub.py`, `tests/service/test_datahub.py`
- **Consumes:** a venue's streaming + candle feeds; the `RateLimiter`.
- **Deliverable:** `MarketDataHub(feed_factory, limiter)`; `subscribe(symbol, timeframe, handler)` and `unsubscribe(...)`. Identical `(symbol, timeframe)` subscriptions share **one** underlying stream; warmup + MTF (`fetch via feed`) go through the limiter and a TTL cache shared across subscribers.
- **Tests:** two subscribers on the same `(symbol, timeframe)` cause **one** underlying fetch/stream (assert call count on a fake feed); cache TTL honored; unsubscribe stops the stream when the last subscriber leaves.

### Task A3 — VenueRegistry + StrategyRegistry + config
- **Files:** `service/registry.py`, `tests/service/test_registry.py`
- **Deliverable:** `build_venue(venue: str, market_type: str, *, creds: dict, live: bool) -> ExecutionVenue` — `venue="coinbase"` + `market_type="spot"` → `CcxtVenue`; `venue="tradovate"` + `market_type="futures"` → `TradovateVenue`. `StrategyRegistry` delegates to the 2D plugin registry (`available_strategies()`, `build_strategy(name, ctx)`). Unknown venue/market_type/strategy → clear error.
- **Tests:** each venue/market_type combo builds the right class (with a fake creds/`from_*` monkeypatched); invalid combos raise.

### Task A4 — RiskGuard (notional caps)
- **Files:** `service/risk.py`, `tests/service/test_risk.py`
- **Deliverable:** `RiskGuard(venue, *, per_bot_cap: float, global_cap: float, global_state)` implementing `ExecutionVenue` (wraps a venue). Before `place_order`, compute resulting notional (spot: `qty × price`; futures: `contracts × venue.contract_multiplier(symbol) × price`) and **block** (return `OrderResult(ok=False, status="risk_blocked")`) if it would exceed the per-bot or shared global cap. Needs a price source (from the MarketDataHub's latest candle). `close`/reduce-only orders always allowed.
- **Tests:** order within cap passes through; order over per-bot cap blocked; over global cap (sum across bots) blocked; reduce-only bypasses.

### Task A5 — Streaming async refactor + BotSupervisor + EventBus
- **Files:** modify `runtime.py`, `stream.py`; add `service/supervisor.py`, `tests/service/test_supervisor.py`
- **Refactor:** add `async def run_async(*symbols)` to `CcxtStreamFeed` (and the `StreamingFeed` protocol); `StreamRuntime` gains an `async def start_async(...)` that awaits the feed loop in the current event loop.
- **Deliverable:** `EventBus` (in-memory pub/sub; `publish(event)`, `subscribe()->async iterator`). `CandleProcessor` gains an optional `on_event` observer callback emitting `Decision`/`OrderEvent`. `BotInstance` = one bot's `{config, runtime, task, status, last_decision, position, pnl}`. `BotSupervisor.create/start/stop/list/get`; each `start` launches the runtime as an `asyncio.Task`; `stop` cancels gracefully (reuse `stop()`).
- **Tests:** supervisor starts/stops a bot (fake feed+venue); two bots run concurrently; EventBus receives a decision event when the fake strategy signals.

### Task A6 — FastAPI API (REST + WS) + auth
- **Files:** `service/api.py`, `service/store.py`, `service/dto.py`, `tests/service/test_api.py`
- **Deliverable:** REST — `POST /bots`, `GET /bots`, `GET /bots/{id}`, `PATCH /bots/{id}` (incl. `live`, risk caps), `POST /bots/{id}/start|stop`, `GET /bots/{id}/trades`, `GET /venues`, `GET /strategies`. `WS /ws` broadcasts EventBus events. Basic auth (seeded users from a config file; session or bearer token). `store.py` persists bot configs (JSON) + trade history (JSONL). Secrets (venue API keys) stored server-side, never returned in responses/logs.
- **Tests:** FastAPI `TestClient` — create→start→get reflects running; PATCH toggles `live`; unauthorized request rejected; `/venues` and `/strategies` list correctly; secrets never appear in any response body.

## Deferred
- SQLite (only if reporting needs queries). Multi-user roles beyond basic auth.
