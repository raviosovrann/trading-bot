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
| `COINBASE_SANDBOX`    | coinbase   | `true`    | Interface parity only — does NOT switch hosts (see note below).   |
| `SYMBOL`              | all        | `BTC/USD` | Market symbol. Alpaca uses `BTC/USD`; Coinbase uses `BTC-USD`.    |
| `TIMEFRAME`           | all        | `5Min`    | Bar timeframe.                                                    |
| `ORDER_QTY`           | all        | `0.001`   | Base-asset order size.                                            |

`require_credentials()` fails fast at startup if the selected real venue is
missing its key/secret. The `fake` venue needs no credentials.

## Important: money safety

> **Alpaca paper (`ALPACA_PAPER=true`) is a genuine risk-free sandbox** with
> real simulated fills. Use it for all testing.
>
> **Coinbase is production / real money.** Coinbase Advanced Trade has **no
> separate REST sandbox**. The `COINBASE_SANDBOX` flag is accepted only for
> interface parity with the other venues — it does **not** switch hosts. Every
> Coinbase call hits the production API (`api.coinbase.com`) and moves real
> funds. Use a dedicated, limited-permission API key and the smallest possible
> `ORDER_QTY` if you run against Coinbase.

**Recommendation:** use Alpaca paper trading for all development and testing.

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

2. **Coinbase CDP keys (real money — use caution):**
   - Create an API key on the Coinbase Developer Platform (CDP).
   - Put them in `.env` as `COINBASE_API_KEY` / `COINBASE_API_SECRET` and set
     `VENUE=coinbase`.
   - Remember: `COINBASE_SANDBOX` does not change anything host-wise; all calls
     are production.

3. **Current limitation — no live datafeed yet.**
   - The live market-data feed for Alpaca/Coinbase is **not implemented**.
     `datafeed.py` currently provides only an in-memory candle feed plus a
     `CandleFeed` protocol and `normalize_candle()` helper.
   - `__main__.py` wires an empty in-memory feed by default, so running the app
     as-is will **not** fetch live bars or emit signals from real market data.
   - A full end-to-end live loop needs the live datafeed work (see Status).

## Status

**Done:**

- Shared models/enums (`models.py`).
- Env-driven config with venue selector and fail-fast credential checks
  (`config.py`).
- `ExecutionVenue` protocol (`venues/base.py`) and in-memory `FakeVenue`
  (`venues/fake.py`).
- Alpaca venue adapter (`venues/alpaca.py`).
- Coinbase venue adapter (`venues/coinbase.py`).
- In-memory candle feed + `CandleFeed` protocol + `normalize_candle`
  (`datafeed.py`).
- Placeholder `SMACrossoverStrategy` (`strategy.py`).
- `SignalRouter` (`router.py`).
- `BotRuntime` + entrypoint wiring (`runtime.py`, `__main__.py`).
- Test suite passing (54 tests, all mocked/faked).

**Remaining:**

- **Live datafeed** for Alpaca/Coinbase market data — the main gap before a
  real end-to-end loop can pull live bars.
- Real strategy port (current SMA crossover is a deterministic placeholder).

## Safety notes

- Keep `.env` local only (it is gitignored — never commit secrets).
- Start with a tiny `ORDER_QTY`.
- Prefer Alpaca paper for testing; treat Coinbase as live money.
