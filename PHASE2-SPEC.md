# Phase 2 — "Trading Console" Design

Status: approved (design), pending spec review
Date: 2026-07-14

## Goal

Turn the working Phase‑1 CLI bot into an operable product for an **internal team**:
a service that runs many bots (multiple strategies, multiple markets) at once,
a broker adapter for **Tradovate crypto futures** (with shorting), a risk cap on
exposure, and a web UI — all cheap to run.

This is a client deliverable, not for our own trading. Capital to fund live
futures is the client's concern; we build so they can validate with a `LIVE=0`
dry‑run before funding.

## Audience & scope

- **Internal team** (a few trusted operators), one shared deployment, basic login.
- Not multi‑tenant SaaS. No public signup, no billing.
- Shared bot state and trade history across operators.

## Architecture

```
React SPA ──REST + WebSocket──▶ FastAPI service (single process, one VM)
                                 ├─ BotSupervisor — one async task per running bot
                                 │     └─ each task = the existing StreamRuntime
                                 ├─ VenueRegistry — ccxt (Coinbase spot, long) │ tradovate (futures, long/short)
                                 ├─ StrategyRegistry — plugin discovery, launch-by-name
                                 ├─ RiskGuard — per-bot + global notional cap (wraps the venue)
                                 ├─ EventBus (in-memory) → WS broadcast of decisions/orders/state
                                 └─ Store — file-based first (configs, trades, users); SQLite later if needed
```

The whole Phase‑1 engine (runtime, router, models, feeds, venue protocol) is
reused untouched behind the `ExecutionVenue` / `Strategy` protocols. Bots are
async tasks sharing memory, so cost is **one small VM regardless of bot count**.

## Sub-projects & build order

Each is its own spec → plan → build cycle. Order:

1. **2C — Tradovate venue** (futures, long/short) — build/test on Tradovate demo env
2. **2A — Service** wrapping the working Coinbase bot (+ EventBus, RiskGuard, supervisor, API)
3. **2D — Refined long/short strategy** (new; concrete AMVR/APTT deleted)
4. **2B — UI** (React SPA) — last; a wrapper over the backend

2C and 2A are independent and could run in parallel; Tradovate leads per priority.

### 2C — Tradovate venue (futures, long + short)

- `TradovateVenue` implements the existing `ExecutionVenue` protocol
  (`place_order`, `close_position`, `get_position`, `health_check`) → drops into
  the VenueRegistry exactly like `CcxtVenue`.
- Adds: OAuth token auth; **demo vs live** env flag (dev/test free on demo);
  micro crypto‑futures contracts (e.g. MBT/MET); futures order types; and
  **long/short** positions (`get_position` returns long/short/flat; `place_order`
  opens either side). The `Signal` model already carries short/sell.
- Market data via Tradovate's market‑data WebSocket (or REST history), adapted to
  our `Candle` / `StreamingFeed`.
- Order size in **contracts**; expose margin/notional for the RiskGuard.
- Tests against a fake Tradovate client and/or the demo environment; no live funds.

### 2A — Bot service + API

- **BotSupervisor**: create / start / stop / list bot instances. Each instance:
  `{id, venue, strategy, symbol, timeframe, size, live, risk_cap, status,
  position, pnl, last_decision}`. Runs the existing runtime as an async task;
  graceful stop reuses the `stop()` already built.
- **RiskGuard** wraps the venue: before any order, compute resulting notional
  (spot: qty × price; futures: contracts × multiplier × price) and **block orders
  that would breach** the per‑bot cap or the global cap across all bots.
- **EventBus**: the runtime already logs decisions; add an observer hook on
  `CandleProcessor` so it also emits decision/order/position events to the bus,
  which the WebSocket broadcasts.
- **REST**: `POST /bots`, `POST /bots/{id}/start|stop`, `GET /bots`,
  `GET /bots/{id}`, `PATCH /bots/{id}` (incl. `live` toggle, risk cap),
  `GET /bots/{id}/trades`, `GET /venues`, `GET /strategies`.
- **WebSocket `/ws`**: live decisions, orders (placed/dry‑run/failed),
  position/PnL, status changes.

### 2D — Refined long/short strategy

- Concrete strategies from Phase 1 (AMVR, and the unbuilt APTT) are **deleted**.
- The refined strategy is designed in its own cycle; it plugs in by name.
- A minimal **reference strategy** stays in the folder so venue/service are
  testable end‑to‑end until the real one lands.

### 2B — UI (React/Next SPA) — last

- Basic login (seeded internal users).
- Dashboard: live table of all bots (symbol, venue, strategy, **LIVE/dry‑run**,
  status, position, PnL, last signal), updated over WS.
- Bot detail: config form, start/stop, live decision log, trade history, small
  price/PnL chart.
- New‑bot wizard: venue → strategy → symbol/timeframe/size, **dry‑run default**;
  the `LIVE` switch is front‑and‑center.
- Served as static files by the FastAPI service (same process, same VM).

## Strategy plugin system

New package `src/tradingbot/strategies/`:

```
strategies/
  __init__.py   # auto-imports every sibling module so @strategy registrations fire
  base.py       # Strategy protocol + StrategyContext (symbol, timeframe, size, data feed, params)
  registry.py   # @strategy(name) decorator; build_strategy(name, ctx); available_strategies()
  example.py    # minimal reference strategy (placeholder until the refined one)
```

- Add a strategy = drop a file, decorate the class with `@strategy("name")`, done.
  It is launchable by that name from config and the UI.
- `build_strategy(name, ctx)` constructs it from a common `StrategyContext`, so
  the service can launch any strategy uniformly; strategy‑specific tuning rides in
  `ctx.params`.

## Cross-cutting

- **Persistence**: file‑based to start — bot configs (JSON/YAML), trade history
  (append‑only JSONL), users (config file). Live state is in‑memory. Introduce
  **SQLite** only when queryable history/reporting is actually needed. No DB server.
- **Auth**: session or JWT, a few seeded internal users, no public signup.
- **Secrets**: venue API keys stored server‑side, **encrypted at rest**, never
  sent to the UI or written to logs. Entered by an operator, not hard‑coded.
- **Risk**: RiskGuard notional caps (per‑bot + global) are mandatory before live
  orders; dry‑run (`LIVE=0`) never sends.

## Cost & deployment

- Single FastAPI process on **one small VM** runs all bots and serves the SPA.
  Bots are I/O‑bound and idle between candles, so **cost is flat regardless of bot
  count** — no per‑bot containers, serverless, or managed DB (the bill‑sprawl traps).
- Cheapest hosts: **Oracle Cloud Free Tier** (always‑free ARM VM, $0), or a
  ~€4/mo Hetzner/Netcup VPS, or ~$5/mo DigitalOcean/Linode.
- Performance is a non‑issue at this scale (a few bots, one decision per candle,
  microsecond math, cached HTF fetches). No premature optimization.

## Testing

- Service: FastAPI TestClient for endpoints; BotSupervisor lifecycle (start/stop,
  concurrent bots); RiskGuard cap enforcement; EventBus broadcast.
- `TradovateVenue`: against a fake client and the demo env.
- Strategy: registry discovery + the reference strategy; refined strategy gets its
  own tests.
- UI: component tests + a couple e2e smokes.
- CI gate stays: tests (3.11/3.12/3.13) + pyright + Bandit + CodeQL.

## Implementation plans

Per‑piece implementation plans live in `doc/` (e.g. `doc/2c-tradovate-plan.md`).

## Deferred / open

- SQLite vs staying file‑based — revisit when reporting needs it.
- Refined strategy specifics — its own design cycle.
- Live Tradovate funding/API subscription — client's call; dev uses demo.
