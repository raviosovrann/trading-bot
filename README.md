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

Generate a secrets-encryption key (venue credentials are encrypted at rest):

```bash
PYTHONPATH=src python -c "from tradingbot.service.crypto import generate_key; print(generate_key())"
export TRADINGBOT_SECRETS_KEY=<the key>   # required whenever the service runs
```

Install the package to get the `tradingbot` admin CLI, then create the first
administrator. The CLI prompts for the password with hidden confirmation and
writes `data/users.json` for you (owner-only permissions) — never hand-edit it:

```bash
pip install -e .                       # provides the `tradingbot` command
tradingbot bootstrap --username admin   # prompts for a password
```

`bootstrap` is one-time and refuses once any user exists. Manage users later
with `tradingbot user add|list|disable|reset-password|revoke-sessions`
(`TRADINGBOT_DATA_DIR` selects the data directory, default `data`). For direct
API access, add a `token_hash` (SHA-256 of a pre-issued token) to a user record.

Start the service (uses the default file-based store under `data/`):

```bash
PYTHONPATH=src TRADINGBOT_SECRETS_KEY=<the key> \
  uvicorn tradingbot.service.main:create_service_app --factory --host 0.0.0.0 --port 8000
```

The file store serializes writers across processes on one POSIX host and
enforces `0700` data directories plus `0600` record files. It deliberately
refuses to start where POSIX file locking is unavailable. This is not a
multi-host database: run one application replica on a persistent volume, or
replace the file store with shared transactional storage before scaling the
service horizontally.

Store venue credentials through the API (they are written encrypted under
`data/secrets.json`; never hand-edit that file):

```bash
curl -X PUT localhost:8000/venues/coinbase/spot/secrets \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"api_key": "...", "api_secret": "...", "api_password": "..."}'
```

## Web UI

The React SPA lives in `ui/` and talks to the service on `:8000` (the dev
server proxies `/api` and `/ws` there).

```bash
cd ui
npm install        # first time only
npm run dev        # http://localhost:5173, log in with your data/users.json password
```

Useful scripts: `npm test` (Vitest), `npm run typecheck`, `npm run lint`,
`npm run build` (production bundle in `ui/dist`, served by the FastAPI
service once 2B task B5 lands).

---

## API

All endpoints except `POST /login` require authentication — a browser session
cookie or an `Authorization: Bearer <token>` API token.

### Auth & secrets

- `POST /login` — `{username, password}` → `{username, roles}`; opens a
  revocable server-side session and sets it in a `Secure; HttpOnly;
  SameSite=Strict` cookie (plus a readable `tb_csrf` companion). No token is
  returned in the body.
- `POST /logout` — revoke the current session and clear its cookies.
- `GET /session` — `{username, roles}` for the authenticated session (used by
  the SPA to restore state); 401 when no session is live.
- `GET /audit` — **admin only**; paginated (`?limit=&before=<seq>`) audit trail
  of sensitive actions with a `chain_ok` tamper-check flag.
- `PUT /venues/{venue}/{market_type}/secrets` — store venue credentials
  (encrypted at rest; never echoed back).

Browser clients authenticate with the session cookie; state-changing requests
must echo the `tb_csrf` cookie in an `X-CSRF-Token` header. Scripts may still use
a long-lived `Authorization: Bearer <token>` API token (from `users.json`'s
`token_hash`), which is exempt from CSRF.

Auth policy (internal-deployment defaults, all environment-tunable):

| Variable | Default | Meaning |
|----------|---------|---------|
| `TRADINGBOT_SESSION_IDLE_TTL` | `1800` | Idle timeout (s) before a session expires. |
| `TRADINGBOT_SESSION_ABSOLUTE_TTL` | `43200` | Absolute session lifetime (s). |
| `TRADINGBOT_LOGIN_MAX_FAILURES` | `5` | Failed logins (per username **and** per IP) before lockout. |
| `TRADINGBOT_LOGIN_LOCKOUT_SECONDS` | `300` | Lockout window (s); further attempts return `429`. |
| `TRADINGBOT_ALLOWED_ORIGINS` | *(same-origin)* | Comma-separated WebSocket origin allowlist. |
| `TRADINGBOT_COOKIE_SECURE` | *(auto)* | Force the cookie `Secure` flag; auto-derives from the request scheme. |

On a rotated or expired session, any `401` (or a `1008` WebSocket auth-close)
centrally clears the SPA's auth state, closes the socket, drops cached data, and
redirects to `/login`; the socket does not reconnect until re-authentication.

Sensitive actions (login success/failure, logout, credential changes, bot
create/start/stop/patch, live-mode toggles, risk-cap changes) are written to an
append-only, hash-chained `data/audit.jsonl` (`0600`). Records are attributed to
a principal, carry a request-correlation id, and are redacted so secrets,
passwords, session ids, and tokens never appear. Read them via admin-only
`GET /api/audit`; retain/rotate the file with your normal backup policy.

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

- `WS /ws` — live decision, order, and position events. Authenticated by the
  session cookie sent on the upgrade (no token in the URL); the `Origin` must
  pass the allowlist (`TRADINGBOT_ALLOWED_ORIGINS`, default same-origin).

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
