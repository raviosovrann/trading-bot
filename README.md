# Trading Bot

An automated crypto trading bot. It watches a market, applies a trading
strategy, and places buy/sell orders on your exchange account — hands-free once
it's running.

Exchange access is powered by [**ccxt**](https://docs.ccxt.com/), so the same
code trades on any ccxt-supported exchange. The default target is **Coinbase**.

---

## What it does

1. Fetches recent price candles from your exchange (REST warmup).
2. In streaming mode, connects to the exchange WebSocket and reacts the instant
   each candle closes (no polling); in single-shot mode it processes the latest
   closed candle once and exits.
3. Runs each closed candle through a strategy (currently a placeholder SMA
   crossover).
4. When the strategy says buy/sell, it places a market order — **or, in dry-run
   mode, just logs the order it *would* place.**

There is no dashboard. The bot logs what it does to the terminal.

---

## Dry-run vs live — the LIVE guard

The bot will not spend real money unless you explicitly opt in.

| `LIVE` | Behaviour |
|--------|-----------|
| unset / `0` (default) | **Dry run.** Fetches data, runs the strategy, and logs the order it would place. Sends nothing to the exchange. Spends **$0**. |
| `1` | **Live.** Places real market orders that move real money. |

**Always verify in dry-run first**, confirm the pipeline behaves, then set
`LIVE=1`.

---

## Setup

### 1. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Create your config

```bash
cp .env.example .env
```

Edit `.env`:

```
EXCHANGE=coinbase
API_KEY=<your key>
API_SECRET=<your secret>
API_PASSWORD=<passphrase — required for Coinbase/OKX/KuCoin>
SYMBOL=DOGE/USD
TIMEFRAME=5m
ORDER_QTY=10
STREAM=0
LIVE=0
```

### 3. Load config into the terminal

```bash
set -a; source .env; set +a
```

Run this in every new terminal before starting the bot.

---

## Getting Coinbase API keys

1. Go to the [Coinbase Developer Platform](https://www.coinbase.com/developer-platform)
   and sign in.
2. Create an API key with **Trade** permission.
3. Copy the key, secret, and passphrase into `API_KEY`, `API_SECRET`, and
   `API_PASSWORD` in `.env`.

To trade a different exchange, set `EXCHANGE` to its
[ccxt id](https://docs.ccxt.com/#/README?id=exchanges) (e.g. `kraken`) and
supply that exchange's credentials.

---

## Running the bot

Single-shot (process one candle, then exit — good for a first smoke test):

```bash
PYTHONPATH=src python3 -m tradingbot
```

Live streaming (event-driven, stays running; Ctrl+C to stop):

```bash
STREAM=1 PYTHONPATH=src python3 -m tradingbot
```

In streaming mode the bot warms up on recent history, connects to the exchange
WebSocket, and acts on each candle as it closes. If the connection drops it
auto-reconnects with exponential backoff and back-fills bars missed during the
outage.

---

## Recommended first run (spend $0)

1. Put real Coinbase keys in `.env` but keep **`LIVE=0`**.
2. Smoke test — single shot:
   ```bash
   set -a; source .env; set +a
   PYTHONPATH=src python3 -m tradingbot
   ```
   Logs should show a candle fetched, a signal or "no signal", and — if a
   signal fired — a **dry-run** order line (nothing sent).
3. Watch streaming for a few minutes with `STREAM=1` (still `LIVE=0`).
4. When you're satisfied, set `LIVE=1` and fund the account with a small
   amount. Cheap alts keep test costs tiny: `DOGE/USD`, `SHIB/USD`, `XRP/USD`,
   `XLM/USD`, `ADA/USD`, `ALGO/USD`, `ARB/USD`.

> The placeholder SMA strategy only trades on a moving-average crossover, so it
> may sit at "no signal" in a flat market. Use a shorter `TIMEFRAME` (e.g. `1m`)
> for quicker activity while testing.

---

## Money safety — read this

- `LIVE=0` (default) never sends an order. Completely risk-free.
- `LIVE=1` places **real orders on a live account**.
- Start with the smallest sensible `ORDER_QTY` on a cheap alt.
- Never commit your `.env` — it's gitignored.
- Ctrl+C stops the bot. Open positions stay on the exchange until you close
  them.

---

## Configuration reference

| Variable | Default | Description |
| --- | --- | --- |
| `EXCHANGE` | `coinbase` | ccxt exchange id (`coinbase`, `kraken`, …). |
| `API_KEY` | *(empty)* | Exchange API key. |
| `API_SECRET` | *(empty)* | Exchange API secret. |
| `API_PASSWORD` | *(empty)* | Passphrase — required by Coinbase/OKX/KuCoin. |
| `SYMBOL` | `BTC/USD` | Market, ccxt unified format (e.g. `DOGE/USD`). |
| `TIMEFRAME` | `5m` | Bar timeframe, ccxt unified (`1m`, `5m`, `15m`, `1h`, `1d`). |
| `ORDER_QTY` | `0.001` | Base-asset order size per signal. |
| `STREAM` | `0` | `1` = event-driven WebSocket streaming; `0` = single-shot. |
| `LIVE` | `0` | `1` = place real orders; `0`/unset = dry-run. |

---

## For developers

<details>
<summary>Architecture, tests, and technical reference</summary>

### Architecture

```text
CcxtCandleFeed / CcxtStreamFeed  ->  Strategy  ->  Router  ->  CcxtVenue  ->  Exchange (ccxt)
            (bars)                    (signals)    (actions)   (orders)
```

Feeds and the venue are the only exchange-specific pieces, and all go through
ccxt. The router and runtime depend only on the `ExecutionVenue` /
`CandleFeed` / `StreamingFeed` protocols.

### Spot long/flat only

This milestone trades **spot**: long or flat, no shorting or leverage.
`CcxtVenue.get_position` derives the position from the base-asset balance.
Futures support is a future drop-in via `fetch_positions()` (a seam is already
in place, keyed on `market_type`).

### Strategy

Current strategy is a placeholder SMA crossover (fast=5, slow=20). Replace
`SMACrossoverStrategy` in `src/tradingbot/strategy.py` with a real strategy — it
only needs to implement the `Strategy` interface. (A TradingView Pine Script
port is planned as the next step.)

### Tests

The suite uses injected fakes only — no live network calls. Test-only doubles
live in `tests/doubles.py` and are not shipped in the package.

```bash
source .venv/bin/activate
pytest -v
```

CI runs the full matrix (3.11/3.12/3.13) plus pyright, Bandit, and CodeQL on
every push and pull request.

</details>
