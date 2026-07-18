# M1 Cluster A — Auth / Identity Redesign

Status: approved 2026-07-17. Covers GitHub issues #140, #115, #139, #131 in the
**M1 - Secure Cloud Foundation** milestone. Delivered as four stacked PRs in the
order #140 → #115 → #139 → #131.

## Goal

Move the trading console from browser-held bearer tokens to revocable,
server-side **cookie sessions**, attach an **authenticated principal** to every
request, keep a durable **audit trail** of sensitive operator actions, and give
operators a safe **first-run bootstrap + user-management CLI** — without breaking
the documented direct-API (bearer-token) workflow used by scripts.

## Current state (baseline)

- `POST /api/login` verifies a PBKDF2 password hash and mints a random bearer
  token, rotating the user's stored `token_hash`. The browser keeps the raw
  token in `sessionStorage`, injects it as `Authorization: Bearer`, and puts it
  in the WebSocket URL (`/ws?token=...`).
- `require_auth` compares the SHA-256 of the presented token against stored
  hashes and returns the **raw token** — there is no identity.
- No server logout, no session expiry, no login throttling, no audit trail.
- Users are created by hand-writing `data/users.json`.
- `BotStore` is a transactional file store (`0700` dir, `0600` files, POSIX
  `flock`, atomic temp-file replace).

## Design decisions

- **Two credential types.** Browsers use **cookie sessions**. Scripts keep using
  **long-lived bearer API tokens** (the existing `token_hash` path), which stay
  a supported, separately-managed credential. Both resolve to a `Principal`.
- **Session lifetimes:** idle timeout **30 min**, absolute lifetime **12 h**
  (configurable via env). Session id rotates on login.
- **CSRF:** double-submit token. Bearer-API requests are CSRF-exempt.
- **WebSocket auth:** session cookie on the upgrade + an **Origin allowlist**.
  No secret in the URL.
- **Roles:** `admin` and `operator`. Only **user-management** is admin-gated in
  M1; every other operator action needs only an authenticated session/token.
- **Audit store:** append-only, hash-chained, redacted JSONL, admin-only read.
- **Packaging:** add `pyproject.toml` (setuptools) exposing a `tradingbot`
  console script for the admin CLI; CI/deploy can `pip install -e .`.

## Data model

`users.json` user record (superset — old records upgrade in place on write):

```json
{
  "id": "<uuid>",
  "username": "operator",
  "password_hash": "pbkdf2_sha256$...",
  "roles": ["operator"],
  "disabled": false,
  "token_hash": "<sha256 of direct-API token, optional>"
}
```

New `sessions.json`, managed by a `SessionStore` (same file-store guarantees):

```json
{
  "sessions": [
    {
      "id_hash": "<sha256 of the random session id>",
      "user_id": "<uuid>",
      "csrf_token": "<random>",
      "created_at": 0,
      "last_seen": 0
    }
  ]
}
```

The raw session id exists only in the browser cookie; only its hash is stored,
so a leaked `sessions.json` cannot be replayed.

## PR 1 — #140 Cookie sessions

- **`SessionStore`** (`service/sessions.py`): `create(user_id) -> raw_id`,
  `resolve(raw_id) -> Session | None` (enforces idle/absolute lifetime, bumps
  `last_seen`), `revoke(raw_id)`, `revoke_user(user_id)`, `csrf_for(raw_id)`.
  Persisted through the existing transactional store patterns.
- **Auth dependency** becomes `require_principal`: resolve session cookie first,
  else `Authorization: Bearer` API token; return a `Principal`. 401 otherwise.
- **Routes:**
  - `POST /api/login` — verify password → create session → `Set-Cookie` session
    (`Secure; HttpOnly; SameSite=Strict; Path=/`) + readable `csrf` cookie →
    return `{username, roles}` (no token in body).
  - `POST /api/logout` — revoke session, clear both cookies.
  - `GET /api/session` — return `{username, roles}` when the session is valid,
    else 401. Lets the SPA restore state without reading a secret.
- **CSRF:** a dependency on state-changing routes (POST/PUT/PATCH/DELETE) that
  requires `X-CSRF-Token` to equal the session's `csrf_token` — **only** when
  the caller authenticated via cookie. Bearer-API callers are exempt.
- **WebSocket `/ws`:** authenticate via the session cookie on the upgrade;
  reject when the `Origin` header is absent/not allowlisted
  (`TRADINGBOT_ALLOWED_ORIGINS`, defaulting to same-origin). No `?token=`.
- **UI:** delete `sessionStorage` token. `useAuth` holds `{username, roles}` and
  restores via `GET /api/session`; `login()`/`logout()` call the endpoints; the
  API client sends `credentials: 'same-origin'` and the CSRF header; the WS
  factory drops the token query param.

Acceptance (#140): no auth secret in `sessionStorage`/`localStorage`/JS; no
token in WS URLs/logs; logout + expiry immediately block REST and WS; CSRF and
cross-origin WS rejected; login distinguishes bad-creds / throttled /
backend-down without leaking username existence; direct-API bearer auth
preserved.

## PR 2 — #115 Hardening (stacks on #140)

- **Login throttle/lockout** (`service/login_guard.py`): per-username **and**
  per-IP failure counters with a lockout window (documented internal-deployment
  policy, env-tunable). Applied in `POST /api/login`; returns 429 while locked.
- **Expiry** already enforced by `SessionStore`; add explicit tests for idle and
  absolute cutoffs.
- **SPA central 401 handling:** the API client raises a typed `Unauthorized`;
  a single handler clears auth state, closes the WS, clears React-Query caches,
  and redirects to `/login`. Stop WS auto-reconnect after an auth close (1008)
  until re-authenticated.
- **Tests:** backend abuse/lockout + "no secret in logs" regression; UI tests
  for rotated/expired session recovery and WS auth-failure handling.

## PR 3 — #139 Principals + audit (stacks on #115)

- **`Principal(id, username, roles, kind)`** returned by `require_principal`;
  `kind` is `user` (session or user-owned token) or `service`.
- **Request-id middleware:** attach/propagate `X-Request-ID` for correlation.
- **`AuditLog`** (`service/audit.py`): append-only `audit.jsonl` (`0600`),
  each record `{seq, ts, prev_hash, hash, actor_id, actor_name, action, target,
  request_id, outcome, before, after}`. `hash = sha256(prev_hash + canonical
  record)` → tamper-evident chain. A redaction pass drops/masks secrets,
  passwords, session ids, tokens, and credential values.
- **Instrumented actions:** login success/failure, logout, secrets change,
  bot create/start/stop/patch, live-mode toggle, risk-cap change, and all user
  management. Records `before`/`after` for mutations (redacted).
- **`GET /api/audit`** — admin-only, paginated (cursor by `seq`), plus a
  chain-verify helper. Retention policy documented in the runbook.

Acceptance (#139): every sensitive mutation attributable to a principal; no
secrets in records; success + failure recorded; records survive restart and are
tamper-evident + access-controlled; tests cover event coverage and redaction.

## PR 4 — #131 Bootstrap + user CLI (stacks on #139)

- **`pyproject.toml`** (setuptools, `src/` layout) with
  `console_scripts: tradingbot = tradingbot.admin:main`.
- **`tradingbot.admin` CLI** (argparse):
  - `bootstrap` — create the first **admin**; refuses once any user exists
    (self-expiring first-run path).
  - `user add|list|disable|reset-password|revoke-sessions`.
  - Passwords read via `getpass` with confirmation; **never** accepted as args
    or logged.
  - Password policy (min length, non-empty) enforced centrally.
  - `reset-password`, `disable`, `revoke-sessions` call
    `SessionStore.revoke_user` so active sessions die immediately.
  - All writes go through the transactional store; no hand-edited JSON.
- **Hash upgrade on login:** when a stored hash uses stale parameters, rehash
  with current parameters on the next successful password verification.

Acceptance (#131): fresh install creates its first operator with no file edits
and no password in shell history/process listing; duplicate/weak/disabled/
malformed cases give clear errors; reset/disable/revoke invalidate sessions;
CLI + first-run flows tested incl. file/dir permissions; runbook documents one
short setup path (delivered fully in #118).

## Error handling

- All auth failures return generic messages (no username-existence leak); login
  always performs constant password work via the existing dummy-hash path.
- Session/audit stores fail closed: unreadable/undecryptable state is treated as
  empty, logged, and (for audit) surfaced by chain verification.
- CSRF/Origin failures return 403; throttled login returns 429; missing/expired
  auth returns 401.

## Testing

- Backend: extend `tests/service/` — `test_sessions.py`, `test_login_guard.py`,
  `test_audit.py`, `test_admin_cli.py`, plus additions to `test_api.py`.
- UI: Vitest for `useAuth`/client/401-handling/WS; Playwright e2e for
  login → action → logout and expiry recovery.
- TDD throughout; each PR keeps `pytest`, `pyright`, and the UI gates green.

## Out of scope (M1 cluster A)

Multi-host/shared-DB session storage, OAuth/SSO, per-endpoint fine-grained RBAC
beyond admin-vs-operator, and audit log shipping to external SIEM.
