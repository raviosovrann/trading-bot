# Monolith Trading Bot Implementation Plan (Alpaca + Coinbase)

**Date:** 2026-07-04
**Goal:** Finish the end-to-end bot base so market data flows through strategy and
router to real exchange execution visible in account activity.

## Architecture target

`DataFeed -> Strategy -> Router -> ExecutionVenue -> Exchange API`

Venues in scope:

- `alpaca` (paper default)
- `coinbase` (sandbox/mainnet selectable)
- `fake` (tests only)

## Constraints

- Python 3.11+
- Pydantic v2
- Secrets only via `.env` / environment variables
- Venue-agnostic router/runtime (depend on `ExecutionVenue` protocol)
- Spot-safe semantics in this milestone (long/flat baseline)

## Task breakdown

### T6 — Alpaca venue adapter

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

### T7 — Coinbase venue adapter

Files:

- `src/tradingbot/venues/coinbase.py` (new)
- `tests/test_coinbase_venue.py` (new)

Deliverables:

- Adapter implementing `ExecutionVenue`.
- Support sandbox/mainnet URL toggle via config.
- Unit tests with mocked Coinbase client responses.

### T8 — Datafeed

Files:

- `src/tradingbot/datafeed.py` (new)
- `tests/test_datafeed.py` (new)

Deliverables:

- Datafeed abstraction + concrete feed path for selected venue.
- Fetch closed bars in normalized `Candle` model.
- Unit tests for parsing/normalization and closed-bar behavior.

### T9 — Strategy placeholder

Files:

- `src/tradingbot/strategy.py` (new)
- `tests/test_strategy.py` (new)

Deliverables:

- `Strategy` interface/protocol
- Placeholder SMA-cross strategy returning signal/no-signal deterministically.

### T10 — Router

Files:

- `src/tradingbot/router.py` (new)
- `tests/test_router.py` (new)

Deliverables:

- Maps strategy signals to venue actions.
- Spot-safe behavior for buy/sell/close and flat/no-position cases.

### T11 — Runtime + entrypoint

Files:

- `src/tradingbot/runtime.py` (new)
- `src/tradingbot/__main__.py` (new)
- `tests/test_runtime.py` (new)

Deliverables:

- Startup config + credential validation for selected venue.
- Wiring: config -> datafeed -> strategy -> router -> venue.
- `run_once` and `run_forever` loop behavior with error resilience.

## Verification gates

- `pytest -v` passes.
- No stale Bybit imports/references in active source/docs.
- Manual smoke run with Alpaca paper and Coinbase sandbox/mainnet settings.
