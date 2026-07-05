# Trading Bot

An automated crypto trading bot. It watches the BTC/USD market, applies a
trading strategy, and places buy/sell orders on your exchange account —
completely hands-free once it's running.

Supported exchanges: **Alpaca** (paper/live) and **Coinbase Advanced Trade**
(sandbox/live).

---

## What it does

Every 5 minutes (configurable) the bot:

1. Fetches the latest completed price candle from your exchange.
2. Runs the price through a strategy (currently: SMA crossover — a simple
   moving-average signal).
3. If the strategy says "buy" or "sell", it places an order on your account.
4. Loops forever until you stop it.

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

### Run once (one market tick, then exit)

```bash
PYTHONPATH=src python3 -m tradingbot
```

Good for testing your setup. You'll see logs showing the candle fetched,
whether a signal fired, and what order (if any) was placed.

### Run forever (live bot loop)

```bash
RUN_FOREVER=1 PYTHONPATH=src python3 -m tradingbot
```

The bot checks for a new closed candle once per second. It only acts when a
new candle has closed — so the actual signal frequency depends on `TIMEFRAME`
(default: every 5 minutes). Stop it with **Ctrl+C**.

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
- **Implementation plans:** `docs/superpowers/plans/`

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
| `RUN_FOREVER`         | all        | `0`       | Set to `1` to run the loop continuously. |

### Tests

The suite uses mocks and fakes only — no live network calls. 78 tests.

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
- CI: full matrix (3.11/3.12/3.13) on every push and pull request

Remaining: real strategy port (pending client's Pine Script source).

</details>
