# Trading Bot

An automated crypto trading bot. It watches the BTC/USD market, applies a
trading strategy, and places buy/sell orders on your exchange account —
completely hands-free once it's running.

Supported exchanges: **Alpaca** (paper/live) and **Coinbase Advanced Trade**
(sandbox/live).

---

## What it does

The bot connects to your exchange's live WebSocket feed and reacts the instant
each price candle closes:

1. Receives the newly closed candle (pushed by the exchange — no polling).
2. Runs it through a strategy (currently: SMA crossover — a simple
   moving-average signal).
3. If the strategy says "buy" or "sell", it places an order on your account.
4. Stays connected and keeps reacting until you stop it (Ctrl+C), auto-reconnecting
   if the connection drops.

It can also run in **single-shot mode** — process the latest candle once and
exit — handy for a quick test or cron-style scheduling.

There is no dashboard or interface. The bot logs what it does to the terminal.

---

## Before you start — pick a mode

| Mode | Exchange | Real money? | Recommended for |
|------|----------|-------------|-----------------|
| **Alpaca paper** | Alpaca | No — simulated fills | First-time testing, development |
| **Coinbase sandbox** | Coinbase | No — fake responses | Checking Coinbase wiring |
| **Alpaca live** | Alpaca | **Yes** | Live trading after testing |
| **Coinbase live** | Coinbase | **Yes** | Live trading after testing |

**Start with Alpaca paper.** It gives you realistic simulated fills with zero
financial risk, and is the fastest way to verify everything works.

---

## Setup

### 1. Install Python dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Create your config file

```bash
cp .env.example .env
```

Open `.env` in any text editor. You'll see something like:

```
VENUE=alpaca
ALPACA_API_KEY=
ALPACA_API_SECRET=
ALPACA_PAPER=true
...
```

Fill in the credentials for the exchange you want to use (see below).

### 3. Load your config into the terminal

```bash
set -a; source .env; set +a
```

Run this command every time you open a new terminal window before starting
the bot. (The `export $(…)` form breaks when `.env` has inline comments, so
this safer form is preferred.)

---

## Getting API keys

### Alpaca (recommended for testing)

1. Go to <https://app.alpaca.markets/> and sign in.
2. In the top-left, switch to **Paper Trading** (not Live).
3. Click **API Keys** → **Generate New Key**.
4. Copy the **Key ID** and **Secret Key** into `.env`:

```
VENUE=alpaca
ALPACA_API_KEY=<paste Key ID here>
ALPACA_API_SECRET=<paste Secret Key here>
ALPACA_PAPER=true        # keep this true for paper trading
```

### Coinbase Advanced Trade

1. Go to <https://www.coinbase.com/developer-platform> and sign in.
2. Create a new API key with **Trade** permission.
3. Copy the API key and secret into `.env`:

```
VENUE=coinbase
COINBASE_API_KEY=<paste here>
COINBASE_API_SECRET=<paste here>
COINBASE_SANDBOX=true    # true = no real money; false = live trades
```

> **Note:** `COINBASE_SANDBOX=true` tests the connection wiring but does not
> produce realistic fills. For realistic testing, use Alpaca paper instead.

---

## Running the bot

### Single-shot (process one candle, then exit)

```bash
PYTHONPATH=src python3 -m tradingbot
```

Good for a quick check of your setup. You'll see logs showing the candle
fetched, whether a signal fired, and what order (if any) was placed.

### Live streaming (event-driven, stays running)

```bash
STREAM=1 PYTHONPATH=src python3 -m tradingbot
```

The bot warms up on recent history, then connects to the exchange WebSocket and
acts on each candle the moment it closes (frequency set by `TIMEFRAME`). If the
connection drops it auto-reconnects with exponential backoff and back-fills any
bars missed during the outage. Press **Ctrl+C** for a clean shutdown.

---

## Manual testing walkthrough (Alpaca paper — recommended)

The fastest way to confirm the whole system works end-to-end, risk-free:

1. **Set up** (once):
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   ```
2. **Add your Alpaca paper keys** to `.env` (see "Getting API keys"); keep
   `VENUE=alpaca` and `ALPACA_PAPER=true`, then load them:
   ```bash
   set -a; source .env; set +a
   ```
3. **Smoke test — single shot** (confirms credentials, data feed, and order path):
   ```bash
   PYTHONPATH=src python3 -m tradingbot
   ```
   The logs should show a candle fetched and either a signal or "no signal".
4. **Go live — streaming.** Leave it running for a few minutes:
   ```bash
   STREAM=1 PYTHONPATH=src python3 -m tradingbot
   ```
   It warms up, connects to the live WebSocket, and acts on each closed candle.
   Stop with **Ctrl+C**.
5. **Verify orders.** Log in at <https://app.alpaca.markets/> → **Paper Trading
   → Orders** — any orders the bot placed appear there.

> Tip: the placeholder SMA strategy only trades on a moving-average crossover,
> so it may sit at "no signal" for a while in a flat market. For quicker
> activity while testing, set a shorter `TIMEFRAME` (e.g. `1Min`).

For Coinbase, set `VENUE=coinbase`, use `BTC-USD` for `SYMBOL`, and keep
`COINBASE_SANDBOX=true` — but the Coinbase sandbox does not produce realistic
fills, so Alpaca paper remains the best end-to-end test.

---

## Money safety — read this

- `ALPACA_PAPER=true` is completely risk-free. No real money moves.
- `COINBASE_SANDBOX=true` sends requests to a test server. No real money moves.
- Changing either to `false` means **real money on a live account**.
- Never commit your `.env` file — it's blocked by `.gitignore`.
- Start with the smallest `ORDER_QTY` (default is `0.001 BTC`, about $60
  at current prices).
- You can stop the bot at any time with Ctrl+C. Open positions stay open on
  the exchange until you close them manually.

---

## Tuning the bot

| Setting | Default | What it controls |
|---------|---------|-----------------|
| `SYMBOL` | `BTC/USD` | Which market to trade (Alpaca format). Use `BTC-USD` for Coinbase. |
| `TIMEFRAME` | `5Min` | How often a new candle closes. Alpaca: any `<N>Min`, `<N>Hour`, or `<N>Day` (e.g. `2Min`, `4Hour`). Coinbase: `1Min`, `5Min`, `15Min`, `30Min`, `1Hour`, `2Hour`, `6Hour`, `1Day`. |
| `ORDER_QTY` | `0.001` | How much BTC to buy/sell per signal. |

---

## Checking it worked

After a run-once or a few minutes of live running:

- **Alpaca paper:** log in at <https://app.alpaca.markets/>, go to
  **Paper Trading → Orders**. You should see the bot's orders listed.
- **Coinbase sandbox:** the sandbox returns mocked responses — no real order
  history to inspect, but the bot logs will show success/failure.
- **Live accounts:** check your Orders or Activity tab on the exchange.

---

## For developers

<details>
<summary>Architecture, tests, and technical reference</summary>

### Architecture

```text
DataFeed  ->  Strategy  ->  Router  ->  ExecutionVenue  ->  Exchange API
(bars)        (signals)     (actions)   (alpaca|coinbase|   (Alpaca /
                                          fake)              Coinbase)
```

The router and runtime depend only on the `ExecutionVenue` protocol — venues
are swappable via config with no code changes.

- **Design spec:** [TradingBot-Design-V3.md](TradingBot-Design-V3.md)
- **Implementation plan:** [doc/implementation-plan.toon](doc/implementation-plan.toon) · [doc/websocket-streaming-plan.md](doc/websocket-streaming-plan.md)

### Configuration reference

| Variable              | Applies to | Default   | Description |
| --------------------- | ---------- | --------- | ----------- |
| `VENUE`               | all        | `alpaca`  | Venue selector: `alpaca`, `coinbase`, or `fake`. |
| `ALPACA_API_KEY`      | alpaca     | *(empty)* | Alpaca API key ID. |
| `ALPACA_API_SECRET`   | alpaca     | *(empty)* | Alpaca API secret key. |
| `ALPACA_PAPER`        | alpaca     | `true`    | `true` = paper trading (risk-free); `false` = live money. |
| `COINBASE_API_KEY`    | coinbase   | *(empty)* | Coinbase CDP API key. |
| `COINBASE_API_SECRET` | coinbase   | *(empty)* | Coinbase CDP API secret. |
| `COINBASE_SANDBOX`    | coinbase   | `true`    | `true` = sandbox host (mocked); `false` = live money. |
| `SYMBOL`              | all        | `BTC/USD` | Market symbol. Alpaca: `BTC/USD`; Coinbase: `BTC-USD`. |
| `TIMEFRAME`           | all        | `5Min`    | Bar timeframe. |
| `ORDER_QTY`           | all        | `0.001`   | Base-asset order size. |
| `STREAM`              | all        | `0`       | `1` = live event-driven WebSocket streaming; `0` = single-shot. |

### Tests

The suite uses mocks and fakes only — no live network calls. 108 tests.

```bash
source .venv/bin/activate
pytest -v
```

### Spot long/flat only

Both venues trade spot in this milestone — long or flat, no shorting or
leverage.

- `buy` opens / increases a long.
- `sell` from a long reduces / closes it.
- `close` flattens the current position.

### Strategy

Current strategy is a placeholder SMA crossover (fast=5 bars, slow=20 bars).
Replace `SMACrossoverStrategy` in `src/tradingbot/strategy.py` to plug in a
real strategy — it only needs to implement the `Strategy` interface.

### Status

All core components done:

- Config, models, enums
- Alpaca venue adapter + Coinbase venue adapter
- Alpaca live datafeed (`AlpacaCandleFeed`) + Coinbase live datafeed (`CoinbaseCandleFeed`)
- `build_feed()` factory wired in `__main__.py`
- SMA crossover strategy placeholder
- Signal router + bot runtime + entrypoint
- Event-driven WebSocket streaming: `StreamRuntime`, `AlpacaStreamFeed` +
  `CoinbaseStreamFeed`, reconnection with exponential backoff + gap-fill, and
  graceful SIGINT/SIGTERM shutdown (the polling `run_forever` loop is retired)
- CI: full matrix (3.11/3.12/3.13) on every push and pull request

Remaining: real strategy port (pending client's Pine Script source).

</details>
