# trading-bot

A single-process Python trading-bot monolith for crypto spot trading on
**Alpaca** and **Coinbase Advanced Trade**. Market data flows through a
deterministic strategy, into a router, and out to a pluggable execution venue.

## Architecture

```text
DataFeed  ->  Strategy  ->  Router  ->  ExecutionVenue  ->  Exchange API
(bars)        (signals)     (actions)   (alpaca|coinbase|   (Alpaca /
                                          fake)              Coinbase)
```

Core rule: the router and runtime depend only on the `ExecutionVenue` protocol,
never on concrete exchange SDK classes. Venues are swappable via config.

- **Design:** [TradingBot-Design-V3.md](TradingBot-Design-V3.md)
- **Implementation plans:** `docs/superpowers/plans/`

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Then edit `.env` and fill in credentials for the venue you selected. To export
the values into your current shell:

```bash
export $(grep -v '^#' .env | xargs)
```

## Configuration

All configuration is environment-driven (see `.env.example`). Pick a venue with
`VENUE` and supply the matching credentials.

| Variable              | Applies to | Default   | Description                                                       |
| --------------------- | ---------- | --------- | ----------------------------------------------------------------- |
| `VENUE`               | all        | `alpaca`  | Venue selector: `alpaca`, `coinbase`, or `fake`.                  |
| `ALPACA_API_KEY`      | alpaca     | *(empty)* | Alpaca API key ID.                                                |
| `ALPACA_API_SECRET`   | alpaca     | *(empty)* | Alpaca API secret key.                                            |
| `ALPACA_PAPER`        | alpaca     | `true`    | `true` = paper trading (risk-free sandbox); `false` = live money. |
| `COINBASE_API_KEY`    | coinbase   | *(empty)* | Coinbase CDP API key.                                             |
| `COINBASE_API_SECRET` | coinbase   | *(empty)* | Coinbase CDP API secret.                                          |
| `COINBASE_SANDBOX`    | coinbase   | `true`    | `true` = sandbox host `api-sandbox.coinbase.com` (mocked); `false` = live money. |
| `SYMBOL`              | all        | `BTC/USD` | Market symbol. Alpaca uses `BTC/USD`; Coinbase uses `BTC-USD`.    |
| `TIMEFRAME`           | all        | `5Min`    | Bar timeframe.                                                    |
| `ORDER_QTY`           | all        | `0.001`   | Base-asset order size.                                            |

`require_credentials()` fails fast at startup if the selected real venue is
missing its key/secret. The `fake` venue needs no credentials.

## Important: money safety

> **Alpaca paper (`ALPACA_PAPER=true`) is a genuine risk-free sandbox** with
> real simulated fills. Use it for all testing.
>
> **Coinbase sandbox (`COINBASE_SANDBOX=true`)** switches the client to the
> Coinbase Advanced Trade sandbox host (`api-sandbox.coinbase.com`). The
> sandbox returns **static/mocked** responses in the same format as production
> for the Accounts and Orders endpoints. It is good for **integration testing**
> of request/response wiring without real money, but it does **not** produce
> realistic fills or PnL.
>
> **Coinbase production (`COINBASE_SANDBOX=false`) is real money.** Every call
> hits `api.coinbase.com` and moves real funds. Use a dedicated,
> limited-permission API key and the smallest possible `ORDER_QTY`.

**Recommendation:** use Alpaca paper trading for realistic risk-free fills
during development; use the Coinbase sandbox for Coinbase integration testing.

## Spot long/flat limitation

Both venues trade **spot** in this milestone, so positions are **long or flat
only** — there is no shorting or leverage.

- `buy` opens / increases a long.
- `sell` from a long reduces / closes it.
- `close` flattens the current position.

For Coinbase, `get_position` is derived from the account base-asset balance and
`entry_price` is reported as `0.0` (the Advanced Trade API does not expose a
spot cost basis here).

## Tests

The suite uses mocks and fakes only — no live network calls.

```bash
pytest -v
```

## Manual testing / human steps

To point the bot at a real account you need to supply credentials yourself.

1. **Alpaca paper keys (recommended for testing):**
   - Sign in at <https://app.alpaca.markets/> and switch to the paper account.
   - Generate an API key/secret under API Keys.
   - Put them in `.env` as `ALPACA_API_KEY` / `ALPACA_API_SECRET`, keep
     `ALPACA_PAPER=true`, and set `VENUE=alpaca`.

2. **Coinbase sandbox (integration testing, no real money):**
   - Create an API key on the Coinbase Developer Platform (CDP).
   - Put them in `.env` as `COINBASE_API_KEY` / `COINBASE_API_SECRET`, set
     `VENUE=coinbase`, and keep `COINBASE_SANDBOX=true`.
   - Requests hit `api-sandbox.coinbase.com`, which returns static/mocked
     responses (Accounts + Orders) in the production format — useful for
     verifying wiring, but not for realistic fills or PnL.

3. **Coinbase production (real money — use caution):**
   - Use the same CDP keys with `VENUE=coinbase` but set
     `COINBASE_SANDBOX=false`.
   - Calls hit `api.coinbase.com` and move real funds — use a
     limited-permission key and the smallest possible `ORDER_QTY`.

4. **Alpaca live datafeed is implemented.** `AlpacaCandleFeed` in
   `datafeed.py` wraps `CryptoHistoricalDataClient` and satisfies the
   `CandleFeed` protocol. `build_feed(cfg)` wires the correct feed by
   venue. `VENUE=alpaca` with real paper keys will now fetch live bars
   and emit signals. Coinbase datafeed is a future task.

## Status

**Done:**

- Shared models/enums (`models.py`).
- Env-driven config with venue selector and fail-fast credential checks
  (`config.py`).
- `ExecutionVenue` protocol (`venues/base.py`) and in-memory `FakeVenue`
  (`venues/fake.py`).
- Alpaca venue adapter (`venues/alpaca.py`).
- Coinbase venue adapter (`venues/coinbase.py`).
- `CandleFeed` protocol + `normalize_candle` + `InMemoryCandleFeed` +
  **`AlpacaCandleFeed`** + `build_feed()` factory (`datafeed.py`).
- Placeholder `SMACrossoverStrategy` (`strategy.py`).
- `SignalRouter` (`router.py`).
- `BotRuntime` + entrypoint wiring (`runtime.py`, `__main__.py`).
- Test suite passing (68 tests, all mocked/faked).

**Remaining:**

- **Coinbase live datafeed** (`CoinbaseCandleFeed` in `datafeed.py`).
- Real strategy port (current SMA crossover is a deterministic placeholder).

## Safety notes

- Keep `.env` local only (it is gitignored — never commit secrets).
- Start with a tiny `ORDER_QTY`.
- Prefer Alpaca paper for testing; treat Coinbase as live money.
