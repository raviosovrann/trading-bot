# trading-bot

A Python bot that runs a BTC-futures strategy against free exchange market data
and executes on **Bybit testnet**. Single process:

```
Bybit data (free) → Python strategy → Router → ExecutionVenue → Bybit testnet
```

**Design:** [`BTC-Futures-TradingBot-Design-V2.md`](BTC-Futures-TradingBot-Design-V2.md)
(current). The original TradingView-webhook design (V1) has been removed; its
webhook layer is no longer part of the project.
Implementation plans live in `docs/superpowers/plans/`.

## Setup

    python3 -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env        # then fill in Bybit testnet keys + settings
    export $(grep -v '^#' .env | xargs)

## Test

    pytest -v

## Run (Bybit testnet)

_Coming with the runtime loop (block B4). The bot pulls market data, runs the
strategy on each closed bar, and routes signals to Bybit testnet._

## Status

- **M0 (done):** project scaffold, `Signal` model, and config (the initial
  webhook receiver has since been removed in the monolith pivot) — all on
  `main` behind CI + CodeQL + PR review.
- **In progress:** execution venue, data feed, strategy port, runtime loop
  (see the V2 design, §12).
