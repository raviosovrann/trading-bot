# Deployment

The maintained deployment artifact is the **container** (`Dockerfile` +
`docker-compose.yml`). It runs as a non-root user, with a read-only application
filesystem and a single writable data volume.

```bash
export TRADINGBOT_SECRETS_KEY="$(python -c 'from tradingbot.service.crypto import generate_key; print(generate_key())')"
export TRADINGBOT_ALLOWED_ORIGINS="https://console.example.com"
docker compose up -d --build
docker compose exec trading-console tradingbot bootstrap --username admin
```

## Health probes

| Endpoint | Purpose | Behaviour |
|----------|---------|-----------|
| `GET /healthz` | Liveness | `200` while the process serves. Restart the container if it fails. |
| `GET /readyz` | Readiness | `200` only when the data directory is writable **and** the secrets key decrypts stored secrets; otherwise `503` with per-dependency detail. |

Route traffic on **`/readyz`**, not `/healthz`: a live process whose store is
unwritable or whose `TRADINGBOT_SECRETS_KEY` is wrong must not receive requests.
Both probes are unauthenticated and leak no configuration values.

## Startup validation

At boot the service re-runs the readiness checks. With
`TRADINGBOT_ENV=production` it **fails closed** (refuses to start) when:

- the data directory is missing/unwritable, or the secrets key is absent or no
  longer decrypts stored secrets (key continuity);
- `TRADINGBOT_ALLOWED_ORIGINS` is unset;
- `TRADINGBOT_COOKIE_SECURE` is explicitly disabled.

Outside production the same problems are logged as prominent errors.

## Reverse proxy, TLS, and headers

The container binds to loopback; a TLS-terminating reverse proxy is the only
public entry point. The proxy must:

- terminate TLS and redirect `http://` → `https://`;
- set `X-Forwarded-Proto: https` and `X-Forwarded-For` (the container runs
  uvicorn with `--proxy-headers`, so `Secure` cookies and client IPs — used by
  the login throttle — are derived correctly);
- restrict `Host` to your operator hostname (reject unknown hosts);
- proxy WebSocket upgrades on `/ws`;
- add security headers:

```nginx
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
add_header X-Content-Type-Options "nosniff" always;
add_header X-Frame-Options "DENY" always;
add_header Referrer-Policy "no-referrer" always;
add_header Content-Security-Policy "default-src 'self'; frame-ancestors 'none'" always;
```

Set `TRADINGBOT_ALLOWED_ORIGINS` to exactly the operator origin(s); the
WebSocket rejects any other `Origin`.

## Resource limits and logs

`docker-compose.yml` caps CPU/memory, drops all capabilities, sets
`no-new-privileges`, and rotates JSON logs (10 MB × 5). The app logs to stdout
unbuffered — ship them with your normal collector. Logs never contain passwords,
session ids, or tokens (covered by a regression test).

## Graceful shutdown

`stop_grace_period: 30s` gives uvicorn time to run the lifespan shutdown, which
stops every running bot and its market-data stream before exit, so no bot is
killed mid-order. Always stop with `docker compose stop` (SIGTERM), never
`kill -9`.

## Backup and restore

All durable state is the single data volume: `bots.json`, `secrets.json`
(encrypted), `users.json`, `sessions.json`, `audit.jsonl`, and `trades/`.

```bash
# Backup (quiesce first so no write is mid-flight)
docker compose stop
docker run --rm -v tradingbot_trading-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/tradingbot-backup.tgz -C /data .
docker compose start

# Restore
docker compose down
docker run --rm -v tradingbot_trading-data:/data -v "$PWD":/backup alpine \
  sh -c "rm -rf /data/* && tar xzf /backup/tradingbot-backup.tgz -C /data"
docker compose up -d
```

**The backup is useless without `TRADINGBOT_SECRETS_KEY`** — `secrets.json` is
encrypted with it. Store the key in a secret manager, separate from the backup.

Run a **restore drill** quarterly: restore into a throwaway volume, start the
service, and confirm `GET /readyz` returns `200` (which proves the key still
decrypts the restored secrets) and that `tradingbot user list` shows the
expected operators. The `deploy-smoke` CI job exercises the clean-install and
backup/restore path automatically on every change.

## Key rotation

`TRADINGBOT_SECRETS_KEY` cannot be swapped in isolation — the old key is needed
to read existing secrets:

1. Back up the data volume (above).
2. With the **old** key set, re-enter each venue credential through
   `PUT /api/venues/{venue}/{market_type}/secrets` after switching to the new
   key, or decrypt-then-re-encrypt offline.
3. Restart with the new key and confirm `/readyz` is `200` — a `503` reporting
   "secrets key cannot decrypt" means continuity was broken; roll back to the
   old key and retry.

Rotate operator credentials with `tradingbot user reset-password`, which also
revokes that user's active sessions.
