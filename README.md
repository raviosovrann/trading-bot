# trading-bot

Python trading-bot monolith for BTC/USD that is being built around:

```text
Market data → Python strategy → Router → ExecutionVenue → Exchange API
```

Current venue targets:

- **Alpaca (paper by default)**
- **Coinbase Advanced Trade (sandbox/mainnet toggle)**

Bybit integration has been removed from this repository.

**Design:** [`TradingBot-Design-V3.md`](TradingBot-Design-V3.md)
**Implementation plans:** `docs/superpowers/plans/`

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
export $(grep -v '^#' .env | xargs)
```

## Configuration

Choose one venue in `.env`:

- `VENUE=alpaca`
  - `SYMBOL` example: `BTC/USD`
  - `TIMEFRAME` example: `5Min`

- `VENUE=coinbase`
  - `SYMBOL` example: `BTC-USD`
  - `TIMEFRAME` currently project-defined; runtime/datafeed implementation sets
    final supported values.

> Coinbase sandbox is useful for API integration checks. The bot still treats
> all venue actions as real trading actions and should be run with small size.

## Test

```bash
pytest -v
```

## Status

- **Done:** base models, config with venue selector, `ExecutionVenue` protocol,
  and `FakeVenue`.
- **In progress:** real venue adapters (Alpaca + Coinbase), datafeed, strategy,
  router, runtime loop, and entrypoint.

## Safety notes

- Keep `.env` local only (never commit secrets).
- Start with tiny `ORDER_QTY`.
- For spot venues, execution semantics are **long/flat** by default in this
  milestone (no leveraged futures/perps behavior).
