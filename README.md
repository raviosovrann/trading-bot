# Trading Console

A multi-bot trading service for internal operators. Runs many bots (multiple
strategies, multiple markets) in a single FastAPI process, with a shared
market-data hub, rate-limited REST, per-bot and global notional risk caps, and
an in-memory event bus that feeds both logs and a live WebSocket.

Supported venues:

- **Coinbase spot** (long-only) via ccxt.
- **Tradovate crypto futures** (long + short) via the Tradovate API.

Everything starts in **dry-run** mode (`LIVE=0`). The service never sends real
orders until an operator explicitly toggles a bot to `live`.

---

## Architecture

```text
React SPA (Phase 2B) ──REST/WebSocket──▶ FastAPI service
                                          ├─ BotSupervisor
                                          ├─ VenueRegistry (coinbase, tradovate)
                                          ├─ StrategyRegistry (pluggable strategies)
                                          ├─ MarketDataHub (shared candles/streams)
                                          ├─ RateLimiter (token bucket per venue)
                                          ├─ RiskGuard (per-bot + global notional caps)
                                          ├─ EventBus ──▶ WebSocket /ws
                                          └─ BotStore (file-based configs + trades)
```

The Phase 1 engine (runtime, router, models, feeds) is reused behind the
`ExecutionVenue` and `Strategy` protocols. Each running bot is one async task
using the existing `StreamRuntime`, so the whole service runs on one small VM
regardless of bot count.

---

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Create `data/users.json` with at least one hashed bearer token:

```json
{
  "users": [
    {"username": "operator", "token_hash": "sha256-of-your-token"}
  ]
}
```

Create `data/secrets.json` with venue credentials (server-side only):

```json
{
  "coinbase": {
    "spot": {
      "api_key": "...",
      "api_secret": "...",
      "api_password": "..."
    }
  },
  "tradovate": {
    "futures": {
      "username": "...",
      "password": "...",
      "client_id": "...",
      "client_secret": "..."
    }
  }
}
```

Start the service:

```bash
PYTHONPATH=src uvicorn tradingbot.service.api:create_app --host 0.0.0.0 --port 8000
```

Or use the provided `run-service.sh` helper once it exists.

---

## API

All endpoints require `Authorization: Bearer <token>`.

### Meta

- `GET /venues` — list supported venue/market-type mappings.
- `GET /strategies` — list registered strategy names.

### Bots

- `POST /bots` — create a bot (dry-run by default).
- `GET /bots` — list all bots.
- `GET /bots/{id}` — get one bot.
- `PATCH /bots/{id}` — update `live`, caps, or params.
- `POST /bots/{id}/start` — start the bot's runtime task.
- `POST /bots/{id}/stop` — stop the bot.
- `GET /bots/{id}/trades` — trade history for the bot.

### WebSocket

- `WS /ws?token=<token>` — live decision, order, and position events.

---

## CLI bot (single-bot, Phase 1 style)

The original single-bot CLI still works for quick testing:

```bash
set -a; source .env; set +a
PYTHONPATH=src python3 -m tradingbot
```

Environment variables:

| Variable | Default | Meaning |
|----------|---------|---------|
| `EXCHANGE` | `coinbase` | ccxt exchange id. |
| `API_KEY` / `API_SECRET` / `API_PASSWORD` | *(empty)* | Coinbase credentials. |
| `SYMBOL` | `BTC/USD` | Market to trade. |
| `TIMEFRAME` | `5m` | Candle size. |
| `ORDER_QTY` | `0.001` | Amount of the base coin per signal. |
| `STRATEGY` | `example` | Strategy name from the registry. |
| `STREAM` | `0` | `1` = stay running; `0` = one-shot. |
| `LIVE` | `0` | `1` = real orders; `0` = dry run. |

---

## Strategy plugins

Add a strategy by dropping a file under `src/tradingbot/strategies/` and
decorating the class:

```python
from __future__ import annotations
from collections.abc import Sequence
from tradingbot.models import Candle, Signal
from tradingbot.strategies.base import StrategyContext
from tradingbot.strategies.registry import strategy

@strategy("mystrategy")
class MyStrategy:
    def __init__(self, ctx: StrategyContext) -> None:
        self.ctx = ctx

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        ...
```

It is automatically discovered and launchable by name from the API.

---

## For developers

```bash
source .venv/bin/activate
pip install -r requirements.txt
pytest -v
```

CI runs the full matrix (Python 3.11/3.12/3.13), `pyright`, Bandit, and
CodeQL on every push and PR.

---

## File layout

- `src/tradingbot/service/` — FastAPI service, supervisor, registry, risk,
  data hub, rate limiter, event bus, store, and DTOs.
- `src/tradingbot/strategies/` — plugin registry and reference strategy.
- `src/tradingbot/venues/` — `ExecutionVenue` implementations.
- `src/tradingbot/` — Phase 1 engine: runtime, router, models, feeds, stream.
- `tests/` — unit and integration tests using fakes; no network or real
  credentials.
