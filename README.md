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

## Manual testing on Bybit testnet

You can test the venue layer against Bybit testnet right now, even before the
full runtime loop is built.

### 1. Get testnet credentials

1. Go to https://testnet.bybit.com → **API Management**.
2. Create an API key for **Unified Trading**.
3. Fund the testnet account with test USDT (testnet faucet / asset transfer).

### 2. Configure the environment

    cp .env.example .env
    # edit .env and paste your testnet key/secret
    export $(grep -v '^#' .env | xargs)

### 3. Run the live venue health check

    python -m pytest tests/test_bybit_venue.py::test_live_testnet_health_check -v

If this passes, your credentials work and the venue can talk to Bybit testnet.

### 4. Run a minimal manual smoke test

Create a scratch script (do not commit it):

```python
import os
from tradingbot.config import load_config, require_bybit_credentials
from tradingbot.venues.bybit import BybitTestnetVenue
from tradingbot.models import Order, Side, OrderType

cfg = load_config(os.environ)
require_bybit_credentials(cfg)
venue = BybitTestnetVenue.from_credentials(
    cfg.bybit_api_key, cfg.bybit_api_secret, testnet=cfg.bybit_testnet
)

assert venue.health_check(), "health check failed"
print("health OK")

# Place a tiny market order on testnet
result = venue.place_order(Order(
    symbol=cfg.symbol,
    side=Side.buy,
    order_type=OrderType.market,
    qty=cfg.order_qty,
))
print(result)

# Read the resulting position
print(venue.get_position(cfg.symbol))

# Close it
print(venue.close_position(cfg.symbol))
```

Run it with:

    python scratch_testnet_smoke.py

> **Warning:** this places real orders on Bybit testnet. Use small `ORDER_QTY`
> and testnet-only credentials.

## Run (Bybit testnet)

_Coming with the runtime loop (block B4). The bot will pull market data, run the
strategy on each closed bar, and route signals to Bybit testnet automatically._

## Status

- **M0 (done):** project scaffold, `Signal` model, and config (the initial
  webhook receiver has since been removed in the monolith pivot) — all on
  `main` behind CI + CodeQL + PR review.
- **Done:** `ExecutionVenue` interface, `FakeVenue`, and `BybitTestnetVenue`
  (PRs #19 and #23).
- **In progress:** data feed, strategy port, router, runtime loop
  (see the V2 design, §12).
