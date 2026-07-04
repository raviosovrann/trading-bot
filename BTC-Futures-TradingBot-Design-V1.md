# BTC Futures Trading Bot — POC Design

> ⚠️ **Superseded by [BTC-Futures-TradingBot-Design-V2.md](BTC-Futures-TradingBot-Design-V2.md)** (2026-07-04).
> The project pivoted from a TradingView Pine + webhook design to a Python
> monolith (exchange data → Python strategy → venue, executing on Bybit testnet).
> This V1 is kept for history; the webhook layer it describes is now **parked/optional**.

**Date:** 2026-07-03
**Status:** Approved (pending spec review)
**Owner:** anvarnosirov98@gmail.com

## 1. Purpose

Build a proof-of-concept bot that executes a TradingView Pine Script strategy for
BTC futures. The strategy fires webhook alerts; the bot receives them and places
the corresponding orders on a trading venue. The POC's success line is a live,
end-to-end pipeline executing on **Bybit testnet**.

The Pine Script strategy/algo itself is out of scope — it is provided by the
client. This project is only the execution bot.

## 2. Goals & Non-Goals

### Goals (POC)
- Receive TradingView webhook alerts reliably and securely.
- Parse and validate the alert payload into a typed `Signal`.
- Execute buy / sell / close orders on Bybit testnet via a venue abstraction.
- Prove correctness by running TradingView's own paper trading in parallel and
  diffing the two records of the same signals.
- Ship a venue-agnostic architecture so NinjaTrader (CME futures) and live
  crypto venues can be added later without touching the core.

### Non-Goals (explicitly deferred)
- Risk management / position sizing logic (quantity is passed explicitly in the
  signal for now).
- Durable state tracking, reconciliation, signal queue/idempotency
  (this is the "B" evolution — see §7).
- NinjaTrader and live-exchange execution (stubbed behind the interface).
- Coinbase (no clean BTC-futures testnet exists; nice-to-have, not required).

## 3. Chosen Approach

**Approach A — thin stateless relay**, structured so **Approach B — queue + worker
+ idempotency** is a drop-in evolution.

- The bot holds essentially no state; the **exchange is the source of truth** for
  positions.
- The `ExecutionVenue` interface is the seam that lets B (and new venues) slot in
  without rewrites. We build the *seams* of B now, but not the queue
  infrastructure (YAGNI).

Rejected: Approach C (full event-driven with reconciliation + risk hooks) — the
eventual production shape, but out of scope for a POC.

## 4. Architecture

```
TradingView (Pine strategy alert, JSON webhook)
        │  HTTPS POST + secret token in body
        ▼
┌─────────────────────────────────────────────┐
│  FastAPI app                                 │
│  1. Webhook endpoint  (auth: token + IP)     │
│  2. SignalParser      (JSON → Signal model)  │
│  3. Router/Executor   (Signal → Order)       │
│  4. ExecutionVenue (interface)               │
│        ├── BybitTestnetVenue   ← built now   │
│        ├── NinjaTraderVenue    ← stub        │
│        └── LiveExchangeVenue   ← stub        │
└─────────────────────────────────────────────┘
```

Each layer has one responsibility, a well-defined interface, and is testable in
isolation.

- **Webhook endpoint** — HTTP concern only: receive POST, enforce auth, hand the
  raw body to the parser. Knows nothing about venues.
- **SignalParser** — turns raw JSON into a validated `Signal` (Pydantic). Rejects
  malformed/unknown payloads with a clear error. Knows nothing about HTTP or
  venues.
- **Router/Executor** — maps a `Signal` to an `Order` and calls the configured
  venue. Knows nothing about *which* venue (selected by config).
- **ExecutionVenue** — the abstraction. One concrete implementation now
  (`BybitTestnetVenue`); others are stubs.

## 5. Data Contracts

### 5.1 Inbound webhook payload (from TradingView `alert_message`)

```json
{
  "token": "long-random-shared-secret",
  "strategy": "btc-futures-v1",
  "action": "buy",            // buy | sell | close
  "symbol": "BTCUSDT",
  "order_type": "market",     // market | limit
  "price": 61250.5,           // optional; for limit / logging
  "quantity": 0.01,           // explicit size (no sizing logic in POC)
  "position_side": "long",    // long | short | flat
  "time": "{{timenow}}"       // TradingView placeholder; used for dedup later
}
```

### 5.2 Internal models

- `Signal` — validated representation of the payload above.
- `Order` — venue-neutral order request (symbol, side, type, qty, optional price).
- `OrderResult` — outcome (venue order id, status, filled qty, raw response).
- `Position` — symbol, side, size, entry price (read back from venue).

### 5.3 ExecutionVenue interface

```python
class ExecutionVenue(Protocol):
    def place_order(self, order: Order) -> OrderResult: ...
    def close_position(self, symbol: str) -> OrderResult: ...
    def get_position(self, symbol: str) -> Position | None: ...
    def health_check(self) -> bool: ...
```

`BybitTestnetVenue` implements it (via `pybit`, v5 unified API, testnet base URL).
`NinjaTraderVenue` and `LiveExchangeVenue` raise `NotImplementedError`.

## 6. Security

TradingView webhook alerts **cannot set custom HTTP headers** — only the URL and
JSON body are controllable. Therefore auth lives in the body, backed by network
controls. Defense in depth:

1. **Secret token in the JSON body** — the primary auth check; constant-time
   comparison. (Optional later upgrade: HMAC where TradingView sends
   `{payload, signature}` and the server verifies `HMAC(secret, payload)`.)
2. **IP allowlist** — reject any source not in TradingView's published webhook IP
   ranges, enforced at the reverse proxy / network layer.
3. **HTTPS** — terminated at the reverse proxy (Caddy/Nginx) or cloud load
   balancer.

Token proves *who*, IP allowlist proves *where from*, HTTPS protects *in transit*.
Secrets live in `.env` (never committed); `config.py` validates them at startup
and fails fast if any are missing.

### Config keys
`WEBHOOK_TOKEN`, `BYBIT_API_KEY`, `BYBIT_API_SECRET`, `VENUE=bybit_testnet`,
`ALLOWED_IPS=...`

## 7. Milestones

- **M0 — Plumbing.** FastAPI `/webhook` receives, validates token, parses
  `Signal`, logs it. Verified with `curl` and a real TradingView alert via a
  tunnel (ngrok / Cloudflare Tunnel).
- **M1 — Execution on testnet (POC "done").** `BybitTestnetVenue.place_order`
  wired; a buy/sell/close alert opens/closes a real position on Bybit testnet.
  **In parallel:** the same Pine strategy is connected to TradingView's own paper
  broker as a *verification oracle* — the two records of identical signals are
  diffed to confirm the bot faithfully mirrors the strategy. (Note: the bot
  cannot execute *into* TradingView paper trading; no external API exists for
  that. It is a reference track only, zero bot code.)
- **M2 — Harden & deploy.** IP allowlist, HTTPS reverse proxy, deploy to a small
  cloud VM, run 24/7.
- **Later (B / C).** Signal queue + idempotency/retry; NinjaTrader (ATI on a
  Windows host) adapter; live crypto venue; risk & state modules.

## 8. Testing

- **SignalParser** — unit tests with valid and malformed/hostile payloads.
- **Router/Executor** — unit tests against a `FakeVenue` asserting the correct
  venue calls for buy/sell/close.
- **Auth** — tests that bad/missing tokens and disallowed IPs are rejected.
- **BybitTestnetVenue** — one integration test against the real testnet
  (place + read back + close).

## 9. Key Constraints & Notes

- **NinjaTrader is not a cloud REST API.** It is a Windows desktop app; automated
  execution goes through its local ATI (order-instruction files or the `NtDirect`
  socket DLL). Its adapter must run on / talk to a Windows host — it cannot be a
  plain cloud POST. Relevant only when M-later NinjaTrader work begins.
- **Bybit** chosen for the first adapter: clean v5 unified API, reliable testnet,
  good Python SDK, fewer geo restrictions than Binance.
- **Stack:** Python + FastAPI (best exchange SDK support; fastest path). Alt:
  Node/TypeScript if the client prefers.
