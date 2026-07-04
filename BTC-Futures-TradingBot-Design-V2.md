# BTC Futures Trading Bot — Design V2 (Python Monolith)

**Date:** 2026-07-04
**Status:** Approved (pivot from V1)
**Owner:** anvarnosirov98@gmail.com
**Supersedes:** `BTC-Futures-TradingBot-Design-V1.md`

## 1. Purpose

A single Python process that runs a BTC-futures strategy (ported from Pine Script
to Python) against **free exchange market data** and executes trades on **Bybit
testnet**. No TradingView dependency, no webhooks, no paid plan.

**Success line:** the bot runs continuously, the strategy consumes live Bybit
market data and emits signals, and buy/sell/close signals open/close real
positions on Bybit testnet.

## 2. Why V2 (what changed from V1)

V1 was: TradingView Pine strategy → webhook alert → receiver bot → venue. That
required a **paid TradingView plan** (webhook alerts are paid) and coupled us to
TradingView's servers.

Rewriting the strategy in Python removes TradingView entirely:
- **Data** comes free from the exchange (Bybit public REST + WebSocket).
- **Signals** are generated in-process — there is nothing to "alert" out, so no
  webhook and no paid plan.
- Everything runs as **one simple app**.

## 3. Scope

**In scope (this build — done "in one go"):**
data feed, strategy engine (Python port), `ExecutionVenue` interface +
`BybitTestnetVenue`, Router, the runtime loop, config, wiring, tests, running on
Bybit testnet.

**Out of scope (explicitly deferred):**
- Cloud deployment.
- Bybit **mainnet / live** trading (testnet only for now).
- Risk management / position sizing beyond what the strategy itself emits.
- Durable state / persistence / a signal queue.
- The V1 **webhook ingress** — parked (see §7).

## 4. Carried over vs parked

- **Carried over from V1/M0:** `Signal` model, `config` loader, and the project
  scaffold (CI + CodeQL + protected-PR pipeline).
- **Parked (dormant, not deleted):** the webhook app (`app.py`, `auth.py`,
  `parser.py`) and its tests. Off the critical path; may be kept later as an
  optional manual/external signal ingress or removed. Its `Signal` contract and
  patterns are reused.

## 5. Architecture

```
Bybit market data (REST history + WS/REST live, free)
        │
        ▼
   DataFeed ──▶ Strategy (Python port of the Pine algo) ──▶ Signal | None
                                                               │
                                                               ▼
                                                            Router ──▶ ExecutionVenue ──▶ Bybit testnet
                                                                          ├─ BybitTestnetVenue  (built)
                                                                          └─ LiveVenue          (later)
```

A single-process **runtime loop** drives it. Each unit has one responsibility and
is testable in isolation.

## 6. Components

1. **config** — extend with `BYBIT_API_KEY`, `BYBIT_API_SECRET`, `SYMBOL`,
   `TIMEFRAME`, and sizing settings. Fail-fast at startup.
2. **models** — reuse `Signal`; add `Order`, `OrderResult`, `Position` for the
   venue layer.
3. **datafeed** — fetch OHLCV candles from Bybit (`pybit`/`ccxt`): historical
   (warm-up + backtest) and live (new closed bars). Knows nothing about strategy
   or execution.
4. **strategy** — a `Strategy` interface (`on_bar(candles) -> Signal | None`) plus
   the ported algo. Deterministic and unit-testable against fixture candle
   series. No I/O.
5. **venues** — `ExecutionVenue` Protocol (`place_order`, `close_position`,
   `get_position`, `health_check`) + `BybitTestnetVenue`.
6. **router** — maps a `Signal` → `Order` → venue call (buy/sell/close →
   venue-specific semantics). Venue-agnostic.
7. **runtime / main** — the loop that wires it together and runs continuously.

## 7. Execution model / data flow

- **Startup:** load + validate config, connect to Bybit testnet
   (`health_check`), fetch historical candles to warm up the strategy's
   indicators.
- **Loop:** on each **new closed bar** (poll REST or subscribe WS), append the
   candle, call `strategy.on_bar(...)`; if it returns a `Signal`, the Router
   executes it on the venue. Log every step (never the API secret).
- **Acting on bar close** avoids Pine-style repaint ambiguity.
- **State:** the exchange is the source of truth (`get_position`); only minimal
   in-memory state is kept.

## 8. Strategy port

- Rewrite Pine → Python. **Not line-for-line** — Pine has specific
  bar-execution/repaint semantics; we match **behavior on closed bars**, not
  source. (Client has accepted this; if parity becomes a serious problem we
  revisit.)
- **Validation / parity check:** compare the Python port against TradingView's
  **free strategy tester** (same symbol/timeframe/date range) — entries, exits,
  and direction should line up. This needs no paid plan.
- **Dependency:** needs the **Pine source**. Until it arrives, build against the
  `Strategy` interface with a simple placeholder (e.g. SMA crossover) so the
  whole pipeline runs end-to-end; drop in the real algo when provided.

## 9. Defaults & open decisions

Sensible defaults (override in config; confirm with client where noted):
- **Symbol:** `BTCUSDT` Bybit **linear perpetual**.
- **Timeframe:** configurable; default `5m`.
- **Position mode:** one-way (not hedge).
- **Sizing:** fixed quantity from config for the POC (no risk module).
- **Parity tolerance:** to confirm with client once the real algo is ported.

## 10. Error handling (POC level)

- Venue/API errors: log and skip that action; do not crash the loop. Idempotent
  retry only where clearly safe.
- Bad/missing data: skip the bar.
- Missing config/secrets: fail fast at startup.

## 11. Testing

- **strategy:** unit tests on fixture candle series (deterministic signals).
- **venues:** one integration test against Bybit testnet (place → read → close).
- **router:** unit tests with a `FakeVenue`.
- **datafeed:** unit test parsing; a light integration test against Bybit public
  data.
- **runtime loop:** integration test wiring the loop with a `FakeVenue` and canned
  candles.

## 12. Build blocks (this milestone)

- **B1 — Execution:** `ExecutionVenue` + `BybitTestnetVenue` + Router. *(needs
  Bybit testnet keys)*
- **B2 — Data:** `DataFeed` (Bybit OHLCV, historical + live).
- **B3 — Strategy:** `Strategy` interface + placeholder; then the real Pine port.
  *(needs Pine source)*
- **B4 — Runtime:** the loop wiring B1–B3; run continuously on Bybit testnet.

## 13. Inputs needed to complete

- **Pine source** — to port the real algo (B3).
- **Bybit testnet API key + secret** — for live testnet execution (B1, B4). Live
  in `.env`, never committed.
