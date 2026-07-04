# Monolith Trading Bot Implementation Plan (Alpaca + Coinbase)

**Date:** 2026-07-04
**Goal:** Finish the end-to-end bot base so market data flows through strategy and
router to real exchange execution visible in account activity.

## Architecture target

`DataFeed -> Strategy -> Router -> ExecutionVenue -> Exchange API`

Venues in scope:

- `alpaca` (paper default — genuine risk-free sandbox)
- `coinbase` (production / real money — `sandbox` flag does NOT switch hosts)
- `fake` (tests only)

## Constraints

- Python 3.11+
- Pydantic v2
- Secrets only via `.env` / environment variables
- Venue-agnostic router/runtime (depend on `ExecutionVenue` protocol)
- Spot-safe semantics in this milestone (long/flat baseline)

## Task breakdown

> **Status:** T6, T7, T9, T10, T11 are **done**, along with the in-memory
> datafeed (`InMemoryCandleFeed` + `CandleFeed` protocol + `normalize_candle`).
> The main remaining gap is a **LIVE datafeed** (Alpaca/Coinbase market data)
> to make the end-to-end loop actually pull real bars.

### T6 — Alpaca venue adapter (DONE)

Files:

- `src/tradingbot/venues/alpaca.py` (new)
- `tests/test_alpaca_venue.py` (new)

Deliverables:

- Adapter implementing `ExecutionVenue` with methods:
  - `place_order`
  - `close_position`
  - `get_position`
  - `health_check`
- Constructor/wiring from API key/secret + paper toggle.
- Unit tests with mocked Alpaca client responses.

### T7 — Coinbase venue adapter (DONE)

Files:

- `src/tradingbot/venues/coinbase.py` (new)
- `tests/test_coinbase_venue.py` (new)

Deliverables:

- Adapter implementing `ExecutionVenue`.
- `COINBASE_SANDBOX` flag accepted for interface parity only — it does NOT
  switch hosts; all calls hit production `api.coinbase.com` (real money).
- Unit tests with mocked Coinbase client responses.

### T8 — Datafeed (in-memory DONE; LIVE feed PENDING)

Files:

- `src/tradingbot/datafeed.py` (new)
- `tests/test_datafeed.py` (new)

Done:

- `InMemoryCandleFeed` + `CandleFeed` protocol + `normalize_candle()`.
- Unit tests for parsing/normalization and closed-bar behavior.

Remaining (main gap):

- **LIVE datafeed** that fetches closed bars from Alpaca/Coinbase market data
  for the active symbol/timeframe, so the runtime loop pulls real bars instead
  of an empty in-memory feed.

### T9 — Strategy placeholder (DONE)

Files:

- `src/tradingbot/strategy.py` (new)
- `tests/test_strategy.py` (new)

Deliverables:

- `Strategy` interface/protocol
- Placeholder SMA-cross strategy returning signal/no-signal deterministically.

### T10 — Router (DONE)

Files:

- `src/tradingbot/router.py` (new)
- `tests/test_router.py` (new)

Deliverables:

- Maps strategy signals to venue actions.
- Spot-safe behavior for buy/sell/close and flat/no-position cases.

### T11 — Runtime + entrypoint (DONE)

Files:

- `src/tradingbot/runtime.py` (new)
- `src/tradingbot/__main__.py` (new)
- `tests/test_runtime.py` (new)

Deliverables:

- Startup config + credential validation for selected venue.
- Wiring: config -> datafeed -> strategy -> router -> venue.
- `run_once` and `run_forever` loop behavior with error resilience.

## Verification gates

- `pytest -v` passes (54 tests, all mocked/faked; no live network).
- No stale Bybit imports/references in active source/docs.
- Manual smoke run possible with Alpaca paper once the LIVE datafeed lands;
  Coinbase runs would be real money (no sandbox host).
