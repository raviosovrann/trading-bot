# Bot Implementation Plan

**Design spec:** [TradingBot-Design-V3.md](../TradingBot-Design-V3.md)

## Task status

| Task | Description | Status |
|------|-------------|--------|
| T6 | Alpaca venue adapter | Done |
| T7 | Coinbase venue adapter | Done |
| T8 | Datafeed (`InMemoryCandleFeed`, `AlpacaCandleFeed`, `CoinbaseCandleFeed`, `build_feed`) | Done |
| T9 | Strategy placeholder (`SMACrossoverStrategy`) | Done |
| T10 | Router (`SignalRouter`) | Done |
| T11 | Runtime + entrypoint (`BotRuntime`, `__main__.py`) | Done |

All 78 tests pass (mocked/faked; no live network).

## What remains

**LT1 — Real strategy port**

Replace the `SMACrossoverStrategy` placeholder with production-grade signal
logic once the client's Pine Script is available. The router and runtime
interfaces are stable and do not need to change.

## Verification gates

- `pytest -v` passes with no live network access.
- `python -m tradingbot` runs end-to-end against Alpaca paper (requires
  `ALPACA_API_KEY` + `ALPACA_API_SECRET` + `ALPACA_PAPER=true`); orders appear
  in the Alpaca paper account dashboard.
- `COINBASE_SANDBOX=true` integration smoke: app wires up cleanly against the
  sandbox host (`api-sandbox.coinbase.com`); note sandbox returns static/mocked
  responses — not realistic fills.
- No Bybit references remain in active source or docs.
