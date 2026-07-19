# Trading Console — Operator & Developer Runbook

The single reference for running, operating, and developing the Trading Console.
Deployment specifics (container, proxy, backups, key rotation) live in
[deployment.md](deployment.md); CI specifics in [ci.md](ci.md).

---

## 1. First-run setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -c constraints.txt
pip install -e .                      # provides the `tradingbot` CLI
```

Generate the secrets-encryption key (venue credentials are encrypted at rest)
and keep it in a secret manager — **without it, stored credentials and backups
are unreadable**:

```bash
python -c "from tradingbot.service.crypto import generate_key; print(generate_key())"
export TRADINGBOT_SECRETS_KEY='<the key>'   # required whenever the service runs
```

Create the first administrator. The CLI prompts for the password with hidden
confirmation and writes `data/users.json` with owner-only permissions — never
hand-edit that file, and never pass a password as an argument:

```bash
tradingbot bootstrap --username admin
```

`bootstrap` is one-time: it refuses once any user exists.

### Managing users

| Command | Effect |
|---------|--------|
| `tradingbot user add --username op [--admin]` | Create an operator (or admin). |
| `tradingbot user list` | Show username, roles, active/disabled, API-token presence. |
| `tradingbot user disable --username op` | Disable the account **and revoke its sessions**. |
| `tradingbot user reset-password --username op` | Set a new password **and revoke its sessions**. |
| `tradingbot user revoke-sessions --username op` | Force re-login everywhere. |

Passwords must be at least 12 characters. A stored hash using stale PBKDF2
parameters is transparently upgraded on the next successful login.

---

## 2. Login and session behaviour

- Login is username + password at `/login`. On success the server creates a
  **server-side session** and sets its random id in a `Secure; HttpOnly;
  SameSite=Strict` cookie. **No token is stored in the browser** — nothing in
  `sessionStorage`, `localStorage`, or reachable from JavaScript.
- Sessions expire after **30 minutes idle** or **12 hours absolute** (whichever
  first). Signing out revokes the session server-side immediately.
- State-changing requests carry an `X-CSRF-Token` header matching the readable
  `tb_csrf` cookie (double-submit CSRF). The UI does this automatically.
- The WebSocket authenticates with the same cookie on the upgrade — **no token
  in the URL** — and its `Origin` must be allow-listed.
- Failed logins are throttled per username **and** per client IP (5 failures →
  5-minute lockout, returning `429`). Errors do not reveal whether a username
  exists.
- If a session expires or is revoked, any `401` (or a WebSocket auth-close)
  clears the UI's auth state, closes the socket, drops cached data, and
  redirects to the login page; the socket stops reconnecting until re-login.

**Direct API access** (scripts) uses a long-lived bearer token instead: add a
`token_hash` (SHA-256 of your chosen token) to a user record and send
`Authorization: Bearer <token>`. Bearer callers are exempt from CSRF.

---

## 3. Running the service

### Development (Vite dev proxy)

Two processes. Backend on `:8000`:

```bash
PYTHONPATH=src TRADINGBOT_SECRETS_KEY='<the key>' \
  uvicorn tradingbot.service.main:create_service_app --factory \
  --host 127.0.0.1 --port 8000
```

UI on `:5173` (proxies `/api` and `/ws` to `:8000`):

```bash
cd ui && npm install   # first time only
npm run dev            # http://localhost:5173
```

### Production bundle (single origin, served by FastAPI)

```bash
cd ui && npm run build         # emits ui/dist
cd .. && PYTHONPATH=src TRADINGBOT_SECRETS_KEY='<the key>' \
  uvicorn tradingbot.service.main:create_service_app --factory \
  --host 127.0.0.1 --port 8000
```

The service serves `ui/dist` at `/` (override with `TRADINGBOT_UI_DIST`) with
SPA deep-link fallback, and the API under `/api`. This is one origin, so
cookies and the WebSocket work without any proxy config.

For the supported container deployment see [deployment.md](deployment.md).

---

## 4. Operating bots

All of this is available in the UI after signing in:

1. **Create** — *New bot* wizard: pick venue + market type, strategy, symbol,
   timeframe, quantity, and per-bot / global notional caps. Venue API
   credentials entered here are stored encrypted server-side. Bots are created
   in **dry-run** mode.
2. **Start / stop** — from the dashboard or the bot detail page. Starting loads
   the stored credentials and attaches the bot to the shared market-data hub.
   Start and stop are **idempotent**: a repeat of either returns `200` and the
   current view rather than an error, and concurrent requests for one bot are
   serialized, so a double-click or a retry cannot create two runtimes or two
   market subscriptions. A failed start releases everything it had partially
   built and can be retried directly.
3. **Switch mode** — toggle `LIVE` on the bot detail page. This always requires
   an explicit confirmation, because it is the switch from logged-only orders to
   **real orders that move real money**.
4. **Set caps** — adjust per-bot and global notional caps on the detail page;
   the risk guard rejects signals that would exceed them.

> **Configuration is immutable while a bot is running.** Mode, caps and strategy
> parameters can only be changed while the bot is **stopped**; `PATCH
> /api/bots/{id}` returns `409` otherwise, and the UI disables those controls
> and says why. The venue, risk guard and strategy are constructed once when the
> bot starts, so a change applied to a running bot would alter only the value
> the API reports, not what actually executes — a `LIVE` toggle that appears to
> work but doesn't is the most dangerous version of that. The supported flow is
> **stop → edit → start**.
5. **Inspect** — the detail page shows position, PnL, the live decision log, and
   trade history. Events also stream live over the WebSocket.
6. **Sign out** — *Sign out* revokes the session server-side.

Equivalent REST endpoints (`POST /api/bots`, `/start`, `/stop`, `PATCH
/api/bots/{id}`, `GET /api/bots/{id}/trades`) are listed in the root README.

### Bot statuses

| Status | Meaning |
|--------|---------|
| `created` | Configured, never started. |
| `starting` | Startup in progress. Lifecycle actions are disabled in the UI and `PATCH` returns `409`. |
| `running` | Trading (or dry-running) against live market data. |
| `stopping` | Shutdown in progress. Same restrictions as `starting`. |
| `stopped` | Cleanly stopped, or restored from disk after a restart. |
| `failed` | Startup or the run loop raised. Partially built resources have been released; start again to retry. |

`starting` and `stopping` are transient. While a bot is in one of them a
configuration change would half-apply — the venue, risk guard and strategy are
built from the config at start — so `PATCH /api/bots/{id}` is rejected with
`409` and the caller should retry once the transition settles.

### Degraded: running but starved of data

A bot also carries a `degraded` flag with a `degraded_reason`, shown in the UI
as a **NO DATA** badge on the dashboard row and a banner on the bot detail page.
It is deliberately *not* a status: a degraded bot is still `running`, and the
lifecycle and `PATCH` rules above are unchanged.

It is set when the market-data stream behind the bot exits without anyone
unsubscribing — the socket dropped, or the venue's watch loop returned. The
runtime stays alive and keeps reporting `running`, but no new bars arrive, so
without this flag the condition is invisible. The reason text carries the
underlying error.

**To recover: stop the bot and start it again.** That rebuilds the stream and
clears the flag. Nothing reconnects automatically today.

### Live state updates

The operator console does not poll. Every lifecycle transition, position
change, PnL move and degradation is broadcast over `WS /ws` as a `state` event
carrying the bot's full authoritative view, so the UI applies it without
refetching. Running bots also re-mark position and PnL every 5 seconds, and the
poll publishes only when the snapshot actually changed, so an idle bot is
silent. Each event carries a per-bot `seq` that increases monotonically; the
client drops any snapshot older than one it has already applied, and resets
that bookkeeping on reconnect so a restarted service is not ignored.

### Backpressure: what happens to a slow client

Each WebSocket subscriber has a **bounded** buffer (256 events). A browser that
stalls briefly catches up losslessly; one that has effectively stopped reading
cannot pin unbounded memory. When a subscriber's buffer fills, events are shed
by kind:

| Event | Under pressure |
|-------|----------------|
| `state` | **Coalesced.** A newer snapshot for the same bot replaces the queued one. Snapshots are complete, so nothing is lost. |
| `decision` | **Dropped, oldest first.** These are informational ticks in a rolling log. |
| `order` | **Never shed to make room for something else.** Orders are also written to the trade log, so an order is never lost — only possibly delayed past the socket. |

Any drop sends the client an `overflow` event carrying the number of events
dropped. The console treats it as "my live view is now incomplete" and refetches
the bot list and trade history rather than carrying on with a partial picture.
**Overflow is therefore always visible, never silent.**

### Slow exchanges cannot freeze the service

ccxt is a synchronous HTTP client, but the service runs one asyncio event loop.
Calling a venue inline means one slow exchange stalls every API request, every
WebSocket and every other bot. All such calls therefore run on worker threads:

| Path | Where it runs |
|------|---------------|
| Candle warmup (REST) | The venue's shared pool, 4 workers. |
| Position / PnL refresh | The venue's shared pool. |
| Bar → strategy → order placement | The **bot's own single-worker lane**. |
| Initial strategy evaluation, gap-fill | The same per-bot lane. |

Two different shapes, for two different reasons:

- **Per venue** (`coinbase:spot`, …) for operator-initiated work, so a stuck
  exchange exhausts only its own four workers and never another venue's.
- **Per bot, single worker** for the trading path, because bars for one bot
  must be processed **in order** — a pool with several workers could reorder
  them — while still isolating a bot stuck in a slow order from every other bot.

Every call has a **20-second deadline**. An overrun is logged and the caller
gives up; a timed-out position refresh leaves the previous value in place
rather than failing the bot.

One caveat to understand before tuning this: **a timeout abandons the wait, not
the thread.** Python cannot interrupt a blocked C call, so the worker stays busy
until the underlying socket gives up. A venue that hangs every call will
therefore saturate its own pool — which is exactly why the pools are per venue,
and why that degradation stays contained.

Worker threads are released on shutdown without waiting, so a hung exchange
cannot delay a restart.

### Trade history: rotation and retention

A bot's orders are written to `data/trades/<bot-id>.<ordinal>.jsonl`. When the
active file reaches **8 MiB** the service starts a new ordinal; it never renames
or rewrites an existing file, so a record's location — and any cursor into it —
stays valid forever.

**Nothing is ever deleted.** There is no retention window and no archival
sweep: the service does not destroy trade records. Disk use therefore grows
with trading activity, and pruning or off-boxing old archives is a deliberate
operator action. `du -sh data/trades` is the number to watch.

The rotation threshold is configurable via `BotStore(trade_rotate_bytes=...)`.
It is a performance knob, not a retention one — it bounds how much must be read
to serve one page of history.

`GET /api/bots/{id}/trades` returns one **bounded page**, newest first:

```
GET /api/bots/{id}/trades?limit=50            -> {"items": [...], "next_cursor": 812}
GET /api/bots/{id}/trades?limit=50&before=812 -> {"items": [...], "next_cursor": null}
```

`limit` is capped at 500 server-side (a larger value is rejected with `422`).
Follow `next_cursor` until it is `null` to walk the whole history. The cursor is
a per-bot `seq` stamped on each record, so paging only ever moves backward into
already-written history — a trade recorded mid-page can neither be duplicated
nor skipped. Records written before this scheme carry no `seq` and are numbered
by their position, which is stable because segments are append-only.

Every sensitive action — login success/failure, logout, credential changes, bot
create/start/stop/update, live-mode toggles, cap changes, user management — is
written to a redacted, hash-chained audit trail readable by an admin at
`GET /api/audit?limit=&before=<seq>`.

---

## 5. Where things live, and watching logs

With the default `TRADINGBOT_DATA_DIR=data` (directory `0700`, files `0600`):

| Path | Contents |
|------|----------|
| `data/users.json` | Operator records: id, username, password hash, roles, disabled flag, optional API-token hash. |
| `data/sessions.json` | Live sessions (hashed ids only). |
| `data/secrets.json` | Venue credentials, **encrypted** with `TRADINGBOT_SECRETS_KEY`. Never hand-edit. |
| `data/bots.json` | Bot configurations (credentials stripped). |
| `data/trades/<bot_id>.jsonl` | Append-only trade/order events per bot. |
| `data/audit.jsonl` | Append-only, hash-chained audit trail. |
| `ui/dist/` | Built SPA served by the service. |

The service logs to stdout. Watch them where you started uvicorn, or:

```bash
docker compose logs -f trading-console     # container
journalctl -u <your-unit> -f               # if wrapped in a systemd unit
```

Logs never contain passwords, session ids, or tokens (enforced by a regression
test). Health: `GET /healthz` (liveness) and `GET /readyz` (readiness — `503`
until the data directory is writable and the secrets key decrypts stored
secrets).

---

## 6. Environment variables

**Service**

| Variable | Default | Meaning |
|----------|---------|---------|
| `TRADINGBOT_SECRETS_KEY` | *(required)* | Fernet key encrypting venue credentials at rest. |
| `TRADINGBOT_DATA_DIR` | `data` | Data directory. |
| `TRADINGBOT_UI_DIST` | `ui/dist` | Built SPA to serve. |
| `TRADINGBOT_ENV` | *(unset)* | `production` enables fail-closed startup validation. |
| `TRADINGBOT_ALLOWED_ORIGINS` | *(same-origin)* | Comma-separated WebSocket origin allowlist. Required in production. |
| `TRADINGBOT_COOKIE_SECURE` | *(auto)* | Force the cookie `Secure` flag; auto-derives from request scheme. |
| `TRADINGBOT_SESSION_IDLE_TTL` | `1800` | Session idle timeout (s). |
| `TRADINGBOT_SESSION_ABSOLUTE_TTL` | `43200` | Absolute session lifetime (s). |
| `TRADINGBOT_LOGIN_MAX_FAILURES` | `5` | Failures (per username and per IP) before lockout. |
| `TRADINGBOT_LOGIN_LOCKOUT_SECONDS` | `300` | Lockout window (s). |

**Single-bot CLI** (`python -m tradingbot`) uses the separate `EXCHANGE`,
`API_KEY`, `API_SECRET`, `API_PASSWORD`, `SYMBOL`, `TIMEFRAME`, `ORDER_QTY`,
`STRATEGY`, `STREAM`, `LIVE` variables documented in the root README.

### Safe key and credential handling

- Keep `TRADINGBOT_SECRETS_KEY` in a secret manager or the process environment —
  never in the repo, an image layer, or a backup archive.
- Enter venue credentials through the UI wizard or
  `PUT /api/venues/{venue}/{market_type}/secrets`; they are encrypted at rest and
  never echoed back or logged.
- `.env` is gitignored — never commit real values.
- Back up `data/` and the key **separately**; see [deployment.md](deployment.md)
  for backup, restore drills, and key rotation.

---

## 7. Current limitations

Read these before trusting the system with money:

- **Tradovate market data is incomplete.** The Tradovate market-data client
  still raises `NotImplementedError` (`src/tradingbot/tradovate_feed.py`), so
  Tradovate bots cannot receive candles. Tracked in #96. Coinbase spot via ccxt
  is the working path.
- **The bundled `example` strategy is a no-op.** It implements the plugin
  lifecycle and always returns `None` (no signals), so a bot running it will
  never trade. It exists to validate wiring; write or plug in a real strategy
  (see the root README's plugin guide). A refined long/short strategy is tracked
  in #78.
- **Starting a bot requires stored venue credentials even in dry-run**, because
  the venue client is constructed before the run loop. Store credentials first;
  in dry-run no orders are sent — the intended order is logged instead.
- **A restart never resumes trading.** Persisted bots are restored from
  `bots.json` on startup, but deliberately come back **stopped** — restart the
  ones you want running. A bot record that cannot be parsed is skipped with a
  warning in the log rather than hiding the rest.
- **Single-host only.** The file store serializes writers on one POSIX host via
  `flock` and refuses to start where POSIX locking is unavailable. Run **one**
  replica on a persistent volume; replace the store with shared transactional
  storage before scaling horizontally.

---

## 8. Verified commands

Python (from the repo root, venv active):

```bash
pytest -v                                             # full suite
pytest --cov=tradingbot --cov-branch \
       --cov-report=term-missing --cov-fail-under=85  # coverage + CI floor
pyright --pythonpath .venv/bin/python src/tradingbot tests
```

> A bare `pyright src/tradingbot tests` may resolve against a different
> interpreter and report spurious missing imports; pass `--pythonpath` (or set
> `pythonPath` in `pyrightconfig.json`) so it uses the repo virtualenv.

UI (from `ui/`):

```bash
npm test           # Vitest unit tests
npm run typecheck  # tsc -b --noEmit
npm run lint       # ESLint
npm run format     # Prettier check (format:fix to write)
npm run build      # production bundle -> ui/dist
npm run e2e        # Playwright smoke (builds the SPA and serves it on :8000)
```

`npm run e2e` uses the repo virtualenv for its backend, so create `.venv` and
install requirements first.
