# Trading Bot — Design V3 (Alpaca + Coinbase Pivot)

**Date:** 2026-07-04
**Status:** Active
**Owner:** <anvarnosirov98@gmail.com>
**Supersedes:** Bybit-centric V2 approach

## 1. Purpose

Build a single Python process that:

1. Pulls market data.
2. Runs a Python strategy on closed bars.
3. Routes signals to a real exchange venue adapter.
4. Places/close orders visible in the selected account.

Primary venues for this milestone:

- **Alpaca** (paper by default)
- **Coinbase Advanced Trade** (sandbox/mainnet selectable)

## 2. Why this pivot

Bybit is region-restricted for this project owner, which blocks both execution
and practical datafeed validation. The project now targets venues with account
access already available to the owner.

## 3. Scope (current milestone)

In scope:

- Exchange-agnostic architecture: `DataFeed -> Strategy -> Router -> ExecutionVenue`
- Venue adapters: Alpaca + Coinbase
- End-to-end runtime loop
- Spot execution baseline with clear long/flat semantics
- Unit + integration-style tests for adapters and routing behavior

Out of scope (deferred):

- Futures/perpetual specific semantics
- Advanced risk engine and portfolio optimizer
- Persistent storage/event sourcing
- Cloud deployment/ops hardening

## 4. Architecture

```text
DataFeed (exchange market data)
    -> Strategy (on closed bars, deterministic)
        -> Signal
            -> Router
                -> ExecutionVenue (alpaca | coinbase | fake)
                    -> Exchange API
```

Core rule: Router and runtime depend only on interfaces, not concrete exchange
SDK classes.

## 5. Components

- `config.py`

  - Venue selector and per-venue credentials/settings.
  - Fail-fast validation for selected venue credentials.

- `models.py`

  - Shared models/enums for candles, signals, orders, positions, results.

- `venues/base.py`

  - `ExecutionVenue` protocol.

- `venues/fake.py`

  - Deterministic in-memory test venue.

- `venues/alpaca.py` (planned)

  - Alpaca adapter implementation.

- `venues/coinbase.py` (planned)

  - Coinbase adapter implementation.

- `datafeed.py` (planned)

  - Market bars for active symbol/timeframe.

- `strategy.py` (planned)

  - Strategy protocol + SMA placeholder (real strategy port later).

- `router.py` (planned)

  - Signal → order/position actions.

- `runtime.py` + `__main__.py` (planned)

  - App wiring and run loop.

## 6. Execution semantics

For this milestone, default behavior is spot-safe:

- `buy` => increase/open long
- `sell` (from long) => reduce/close long
- `close` => flatten current position

No leverage-specific assumptions are made.

## 7. Data and strategy behavior

- Act on **closed bars** only.
- Keep strategy deterministic and testable with candle fixtures.
- Strategy implementation can be replaced later without changing router/runtime.

## 8. Error handling

- Missing/invalid credentials for selected venue: fail fast at startup.
- Venue/API action failures: return structured `OrderResult` with `ok=False` and
  preserve raw response when available.
- Runtime loop should continue safely on transient data/venue errors.

## 9. Testing strategy

- Unit tests for config/model/router logic.
- Venue adapter tests with mocked SDK/client responses.
- Runtime integration test with `FakeVenue` + canned candles.
- Optional live smoke checks for Alpaca paper and Coinbase sandbox/mainnet.

## 10. Milestone completion definition

The base bot is considered wrapped up when:

- Datafeed supplies bars continuously.
- Strategy emits signals from those bars.
- Router converts signals into venue actions.
- Orders are executed through selected venue API and visible in the venue account.
