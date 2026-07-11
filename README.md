# Trading Bot

An automated crypto trading bot for **Coinbase spot** (via [ccxt](https://docs.ccxt.com/)).
It runs one strategy — the **Adaptive Momentum Velocity Ribbon (AMVR)** — on a
market you choose, and places buy/sell orders for you.

It is **long-only** (spot: you're either holding the coin or in cash) and won't
spend a cent until you explicitly turn trading on.

---

## The strategy in one paragraph

AMVR tracks three Hull moving averages (the "ribbon") and measures how fast each
is rising. It **buys** when momentum lines up in your favour — a bullish
"prepare" signal has fired, all three ribbons are green (rising), momentum is
accelerating up, and both the **1H and 4H** timeframes are also accelerating up.
It **sells everything back to cash** when momentum rolls over (the bearish
"prepare" signal). It never shorts.

Because all four conditions must align, it trades selectively — it will often sit
in cash waiting, which is by design.

---

## Dry run vs live — the safety switch

| `LIVE` | What happens |
|--------|--------------|
| `0` (default) | **Dry run.** Reads real market data, decides, and *logs the order it would place*. Sends nothing. Costs **$0**. |
| `1` | **Live.** Places real orders with real money. |

Always run in dry run first.

---

## Setup (once)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Then get Coinbase API keys:

1. Go to the [Coinbase Developer Platform](https://www.coinbase.com/developer-platform), sign in.
2. Create an API key with **Trade** permission.
3. Put the key, secret, and passphrase into `.env`.

Your `.env` should look like:

```
EXCHANGE=coinbase
API_KEY=<your key>
API_SECRET=<your secret>
API_PASSWORD=<your passphrase>
SYMBOL=XRP/USD          # any cheap alt: DOGE/USD, XRP/USD, ADA/USD, ...
TIMEFRAME=1h            # bar size the ribbon runs on
ORDER_QTY=25            # how much of the coin to buy per signal
STREAM=1                # 1 = stay running and react live; 0 = one-shot
LIVE=0                  # keep 0 until you're ready to trade real money
```

---

## Running it

Load your config into the shell, then start the bot:

```bash
set -a; source .env; set +a
PYTHONPATH=src python3 -m tradingbot
```

- With `STREAM=1` it warms up on history, then stays connected and reacts to each
  candle as it closes. **Ctrl+C** to stop.
- With `STREAM=0` it checks the latest candle once and exits (good for a quick test).

---

## Recommended path with a funded account ($50–100)

1. **Dry run first.** Keep `LIVE=0`. Run it and watch the logs decide over a few
   hours across a couple of symbols. Right now it will mostly sit flat — normal.
2. **Pick a cheap alt** so each trade is small: e.g. `SYMBOL=XRP/USD`.
3. **Size the order** with `ORDER_QTY` — this is the amount of the *coin*, not
   dollars. For ~$25 of XRP at ~$1.10, use `ORDER_QTY=22`. Start small.
4. **Go live:** set `LIVE=1`, reload (`set -a; source .env; set +a`), and run.
5. **Verify** on Coinbase → your account's Orders/Activity that fills appear.

> With ~$98 in the account, keep `ORDER_QTY` well under your balance so a buy
> always has room, and expect the bot to wait for momentum to align before its
> first trade.

---

## Configuration reference

| Variable | Default | Meaning |
|----------|---------|---------|
| `EXCHANGE` | `coinbase` | ccxt exchange id. |
| `API_KEY` / `API_SECRET` / `API_PASSWORD` | *(empty)* | Coinbase credentials (passphrase required). |
| `SYMBOL` | `BTC/USD` | Market to trade (e.g. `XRP/USD`). |
| `TIMEFRAME` | `5m` | Candle size the ribbon runs on (`1m`,`5m`,`15m`,`1h`,`4h`,`1d`). |
| `ORDER_QTY` | `0.001` | Amount of the base coin to buy per signal. |
| `STREAM` | `0` | `1` = stay running and react live; `0` = one-shot. |
| `LIVE` | `0` | `1` = real orders; `0` = dry run. |

---

## For developers

<details>
<summary>Architecture & tests</summary>

```text
CcxtCandleFeed / CcxtStreamFeed  ->  AMVR strategy  ->  Router  ->  CcxtVenue  ->  Coinbase (ccxt)
        (candles)                    (buy/sell)         (routes)    (orders)
```

- Strategy: `src/tradingbot/amvr.py` (`AdaptiveMomentumRibbonStrategy`). The 1H/4H
  momentum is fetched via an injected `CcxtCandleFeed` (cached, and resilient to
  transient REST failures — a fetch error blocks entry rather than crashing).
- Spot long/flat only; `CcxtVenue.get_position` reads the base-asset balance.
- Known limitation: the strategy tracks its own in/out-of-position state, which
  can drift from the real venue position after a rejected/partial order or a
  manual trade. Reconciling against `get_position()` is a planned follow-up.

```bash
source .venv/bin/activate
pip install -r requirements.txt
pytest -v
```

CI runs the full matrix (3.11/3.12/3.13) plus pyright, Bandit, and CodeQL on every push and PR.

</details>
