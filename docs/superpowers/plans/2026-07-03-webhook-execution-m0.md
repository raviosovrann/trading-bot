# M0 — Webhook Plumbing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a secured FastAPI webhook that receives a TradingView alert, authenticates it, validates it into a typed `Signal`, and logs it — proving the signal pipeline end-to-end.

**Architecture:** Thin stateless FastAPI relay (Approach A). Layers are isolated and independently testable: config → models → parser → auth → webhook app. The file layout reserves a `venues/` package and a router seam so M1's `BybitTestnetVenue` drops onto the same skeleton without touching M0 code.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic v2, Uvicorn, pytest, httpx (TestClient). Dependency install via `venv` + `requirements.txt`.

## Global Constraints

- Python 3.11+ (uses `X | None` union syntax and `tuple[...]` generics).
- Pydantic v2 API (`model_validate`, `field_validator`).
- Secrets only via environment / `.env`; `.env` is gitignored and never committed.
- The webhook token is NEVER written to logs.
- Auth lives in the JSON body (TradingView cannot set custom HTTP headers).
- Token comparison uses `hmac.compare_digest` (constant-time).
- All code under `src/tradingbot/`; all tests under `tests/`.

---

## File Structure

```
trading-bot/
├── requirements.txt              # runtime + dev deps
├── .env.example                  # documented env template (committed)
├── pytest.ini                    # pytest config (pythonpath=src)
├── src/
│   └── tradingbot/
│       ├── __init__.py
│       ├── config.py             # load_config() → Config; fail-fast validation
│       ├── models.py             # Signal (+ Action/OrderType/PositionSide enums)
│       ├── parser.py             # parse_signal(dict) → Signal; SignalParseError
│       ├── auth.py               # is_authorized(), ip_allowed()
│       └── app.py                # create_app(config) → FastAPI; /webhook, /health
└── tests/
    ├── test_config.py
    ├── test_models.py
    ├── test_auth.py
    └── test_webhook.py
```

**Reserved for M1 (not created in M0, listed so the skeleton is understood):**
```
src/tradingbot/
    router.py                     # Router: Signal → venue.place_order(...)
    venues/
        __init__.py
        base.py                   # ExecutionVenue Protocol + Order/OrderResult/Position
        bybit.py                  # BybitTestnetVenue
```
In M0 the webhook logs the validated `Signal` and returns. In M1, one line in
`app.py` swaps the log for `router.handle(signal)` — no other M0 file changes.

**Responsibilities (one job each):**
- `config.py` — read & validate environment, fail fast. Knows nothing about HTTP.
- `models.py` — the typed shape of a signal and its enums. Pure data.
- `parser.py` — dict → validated `Signal`, or a clean error. No HTTP, no venue.
- `auth.py` — token check + IP allowlist check. Pure functions.
- `app.py` — HTTP wiring only: receive, authorize, parse, log, respond.

---

### Task 1: Project scaffold & config

**Files:**
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `pytest.ini`
- Create: `src/tradingbot/__init__.py` (empty)
- Create: `src/tradingbot/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class Config` — frozen dataclass with fields `webhook_token: str`, `venue: str`, `allowed_ips: tuple[str, ...]`.
  - `class ConfigError(RuntimeError)`.
  - `load_config(env: dict[str, str] | None = None) -> Config` — reads from `env` (defaults to `os.environ`); raises `ConfigError` if `WEBHOOK_TOKEN` missing/empty.

- [ ] **Step 1: Create dependency + tooling files**

`requirements.txt`:
```
fastapi==0.115.6
uvicorn[standard]==0.34.0
pydantic==2.10.4
httpx==0.28.1
pytest==8.3.4
```

`.env.example`:
```
# Copy to .env and fill in. .env is gitignored — never commit real values.
WEBHOOK_TOKEN=replace-with-a-long-random-secret
VENUE=bybit_testnet
# Comma-separated. Empty = allow all (dev only). TradingView IPs set in M2.
ALLOWED_IPS=
```

`pytest.ini`:
```ini
[pytest]
pythonpath = src
testpaths = tests
```

- [ ] **Step 2: Set up the virtualenv and install deps**

Run:
```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
```
Expected: installs without error; `python -c "import fastapi, pydantic, pytest"` prints nothing (success).

- [ ] **Step 3: Write the failing test**

`tests/test_config.py`:
```python
import pytest
from tradingbot.config import load_config, Config, ConfigError


def test_load_config_reads_values():
    cfg = load_config({"WEBHOOK_TOKEN": "secret", "VENUE": "bybit_testnet", "ALLOWED_IPS": "1.2.3.4, 5.6.7.8"})
    assert isinstance(cfg, Config)
    assert cfg.webhook_token == "secret"
    assert cfg.venue == "bybit_testnet"
    assert cfg.allowed_ips == ("1.2.3.4", "5.6.7.8")


def test_venue_defaults_when_absent():
    cfg = load_config({"WEBHOOK_TOKEN": "secret"})
    assert cfg.venue == "bybit_testnet"
    assert cfg.allowed_ips == ()


def test_missing_token_raises():
    with pytest.raises(ConfigError):
        load_config({"VENUE": "bybit_testnet"})
```

- [ ] **Step 4: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tradingbot.config'`.

- [ ] **Step 5: Write minimal implementation**

`src/tradingbot/config.py`:
```python
import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    webhook_token: str
    venue: str
    allowed_ips: tuple[str, ...]


def load_config(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ) if env is None else env
    if not env.get("WEBHOOK_TOKEN"):
        raise ConfigError("Missing required env var: WEBHOOK_TOKEN")
    allowed = tuple(
        ip.strip() for ip in env.get("ALLOWED_IPS", "").split(",") if ip.strip()
    )
    return Config(
        webhook_token=env["WEBHOOK_TOKEN"],
        venue=env.get("VENUE", "bybit_testnet"),
        allowed_ips=allowed,
    )
```

`src/tradingbot/__init__.py`: empty file.

- [ ] **Step 6: Run test to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: PASS (3 passed).

- [ ] **Step 7: Commit**

```bash
git add requirements.txt .env.example pytest.ini src/tradingbot/__init__.py src/tradingbot/config.py tests/test_config.py
git commit -m "feat: project scaffold and config loader"
```

---

### Task 2: Signal model & parser

**Files:**
- Create: `src/tradingbot/models.py`
- Create: `src/tradingbot/parser.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - Enums `Action` (`buy|sell|close`), `OrderType` (`market|limit`), `PositionSide` (`long|short|flat`) — all `str`-valued.
  - `class Signal(BaseModel)` with fields: `token: str`, `strategy: str`, `action: Action`, `symbol: str`, `order_type: OrderType = OrderType.market`, `price: float | None = None`, `quantity: float`, `position_side: PositionSide`, `time: str | None = None`.
  - `class SignalParseError(ValueError)`.
  - `parse_signal(payload: dict) -> Signal` — validates; raises `SignalParseError` on any invalid/missing field.

- [ ] **Step 1: Write the failing test**

`tests/test_models.py`:
```python
import pytest
from tradingbot.models import Signal, Action, OrderType, PositionSide
from tradingbot.parser import parse_signal, SignalParseError


def _valid_payload():
    return {
        "token": "secret",
        "strategy": "btc-futures-v1",
        "action": "buy",
        "symbol": "BTCUSDT",
        "order_type": "market",
        "price": 61250.5,
        "quantity": 0.01,
        "position_side": "long",
        "time": "1720000000",
    }


def test_parse_valid_signal():
    sig = parse_signal(_valid_payload())
    assert isinstance(sig, Signal)
    assert sig.action is Action.buy
    assert sig.order_type is OrderType.market
    assert sig.position_side is PositionSide.long
    assert sig.symbol == "BTCUSDT"
    assert sig.quantity == 0.01


def test_order_type_defaults_to_market():
    payload = _valid_payload()
    del payload["order_type"]
    assert parse_signal(payload).order_type is OrderType.market


def test_missing_required_field_raises():
    payload = _valid_payload()
    del payload["action"]
    with pytest.raises(SignalParseError):
        parse_signal(payload)


def test_invalid_action_raises():
    payload = _valid_payload()
    payload["action"] = "hodl"
    with pytest.raises(SignalParseError):
        parse_signal(payload)


def test_non_positive_quantity_raises():
    payload = _valid_payload()
    payload["quantity"] = 0
    with pytest.raises(SignalParseError):
        parse_signal(payload)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tradingbot.models'`.

- [ ] **Step 3: Write minimal implementation**

`src/tradingbot/models.py`:
```python
from enum import Enum
from pydantic import BaseModel, field_validator


class Action(str, Enum):
    buy = "buy"
    sell = "sell"
    close = "close"


class OrderType(str, Enum):
    market = "market"
    limit = "limit"


class PositionSide(str, Enum):
    long = "long"
    short = "short"
    flat = "flat"


class Signal(BaseModel):
    token: str
    strategy: str
    action: Action
    symbol: str
    order_type: OrderType = OrderType.market
    price: float | None = None
    quantity: float
    position_side: PositionSide
    time: str | None = None

    @field_validator("quantity")
    @classmethod
    def _quantity_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("quantity must be > 0")
        return v
```

`src/tradingbot/parser.py`:
```python
from pydantic import ValidationError

from .models import Signal


class SignalParseError(ValueError):
    pass


def parse_signal(payload: dict) -> Signal:
    try:
        return Signal.model_validate(payload)
    except ValidationError as e:
        raise SignalParseError(str(e)) from e
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_models.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/tradingbot/models.py src/tradingbot/parser.py tests/test_models.py
git commit -m "feat: signal model and parser with validation"
```

---

### Task 3: Auth (token + IP allowlist)

**Files:**
- Create: `src/tradingbot/auth.py`
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `is_authorized(provided_token: str | None, expected_token: str) -> bool` — constant-time compare; `False` if provided is falsy.
  - `ip_allowed(client_ip: str, allowed_ips: tuple[str, ...]) -> bool` — empty allowlist ⇒ `True` (dev); otherwise membership check.

- [ ] **Step 1: Write the failing test**

`tests/test_auth.py`:
```python
from tradingbot.auth import is_authorized, ip_allowed


def test_correct_token_authorized():
    assert is_authorized("secret", "secret") is True


def test_wrong_token_rejected():
    assert is_authorized("nope", "secret") is False


def test_missing_token_rejected():
    assert is_authorized(None, "secret") is False
    assert is_authorized("", "secret") is False


def test_empty_allowlist_allows_all():
    assert ip_allowed("9.9.9.9", ()) is True


def test_allowlisted_ip_allowed():
    assert ip_allowed("1.2.3.4", ("1.2.3.4", "5.6.7.8")) is True


def test_non_allowlisted_ip_rejected():
    assert ip_allowed("9.9.9.9", ("1.2.3.4",)) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tradingbot.auth'`.

- [ ] **Step 3: Write minimal implementation**

`src/tradingbot/auth.py`:
```python
import hmac


def is_authorized(provided_token: str | None, expected_token: str) -> bool:
    if not provided_token:
        return False
    return hmac.compare_digest(provided_token, expected_token)


def ip_allowed(client_ip: str, allowed_ips: tuple[str, ...]) -> bool:
    if not allowed_ips:
        return True
    return client_ip in allowed_ips
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_auth.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add src/tradingbot/auth.py tests/test_auth.py
git commit -m "feat: token and IP allowlist auth helpers"
```

---

### Task 4: FastAPI webhook endpoint

**Files:**
- Create: `src/tradingbot/app.py`
- Test: `tests/test_webhook.py`

**Interfaces:**
- Consumes: `load_config`/`Config` (Task 1), `parse_signal`/`SignalParseError` (Task 2), `is_authorized`/`ip_allowed` (Task 3), `Signal` (Task 2).
- Produces:
  - `create_app(config: Config | None = None) -> FastAPI` — app factory; if `config` is `None`, calls `load_config()`.
  - `POST /webhook` — 200 `{"status":"received", ...}` on success; 403 disallowed IP; 400 bad JSON; 401 bad/missing token; 422 invalid signal.
  - `GET /health` — 200 `{"status":"ok","venue":<venue>}`.
  - Logging: on success logs action/symbol/quantity/side/strategy — NEVER the token.

- [ ] **Step 1: Write the failing test**

`tests/test_webhook.py`:
```python
import pytest
from fastapi.testclient import TestClient

from tradingbot.config import Config
from tradingbot.app import create_app


def _payload(**over):
    p = {
        "token": "secret",
        "strategy": "btc-futures-v1",
        "action": "buy",
        "symbol": "BTCUSDT",
        "order_type": "market",
        "price": 61250.5,
        "quantity": 0.01,
        "position_side": "long",
        "time": "1720000000",
    }
    p.update(over)
    return p


@pytest.fixture
def client():
    cfg = Config(webhook_token="secret", venue="bybit_testnet", allowed_ips=())
    return TestClient(create_app(cfg))


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "venue": "bybit_testnet"}


def test_valid_webhook_accepted(client):
    r = client.post("/webhook", json=_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "received"
    assert body["symbol"] == "BTCUSDT"


def test_bad_token_rejected(client):
    r = client.post("/webhook", json=_payload(token="wrong"))
    assert r.status_code == 401


def test_missing_token_rejected(client):
    p = _payload()
    del p["token"]
    r = client.post("/webhook", json=p)
    assert r.status_code == 401


def test_invalid_signal_rejected(client):
    r = client.post("/webhook", json=_payload(action="hodl"))
    assert r.status_code == 422


def test_non_dict_body_rejected(client):
    r = client.post("/webhook", json=[1, 2, 3])
    assert r.status_code == 401


def test_ip_allowlist_blocks(client):
    cfg = Config(webhook_token="secret", venue="bybit_testnet", allowed_ips=("8.8.8.8",))
    blocked = TestClient(create_app(cfg))
    r = blocked.post("/webhook", json=_payload())
    assert r.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_webhook.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tradingbot.app'`.

- [ ] **Step 3: Write minimal implementation**

`src/tradingbot/app.py`:
```python
import logging

from fastapi import FastAPI, HTTPException, Request

from .auth import ip_allowed, is_authorized
from .config import Config, load_config
from .parser import SignalParseError, parse_signal

logger = logging.getLogger("tradingbot")


def create_app(config: Config | None = None) -> FastAPI:
    config = config or load_config()
    app = FastAPI(title="TradingBot Webhook")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "venue": config.venue}

    @app.post("/webhook")
    async def webhook(request: Request) -> dict:
        client_ip = request.client.host if request.client else ""
        if not ip_allowed(client_ip, config.allowed_ips):
            logger.warning("Rejected webhook from disallowed IP: %s", client_ip)
            raise HTTPException(status_code=403, detail="IP not allowed")

        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        token = payload.get("token") if isinstance(payload, dict) else None
        if not is_authorized(token, config.webhook_token):
            logger.warning("Rejected webhook: bad/missing token from %s", client_ip)
            raise HTTPException(status_code=401, detail="Unauthorized")

        try:
            signal = parse_signal(payload)
        except SignalParseError as e:
            logger.warning("Rejected webhook: invalid signal: %s", e)
            raise HTTPException(status_code=422, detail="Invalid signal")

        # M0: log only. M1 swaps this line for router.handle(signal).
        logger.info(
            "Signal received: action=%s symbol=%s qty=%s side=%s strategy=%s",
            signal.action.value,
            signal.symbol,
            signal.quantity,
            signal.position_side.value,
            signal.strategy,
        )
        return {
            "status": "received",
            "action": signal.action.value,
            "symbol": signal.symbol,
        }

    return app
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_webhook.py -v`
Expected: PASS (7 passed).

- [ ] **Step 5: Run the full suite**

Run: `pytest -v`
Expected: PASS (21 passed total).

- [ ] **Step 6: Commit**

```bash
git add src/tradingbot/app.py tests/test_webhook.py
git commit -m "feat: secured webhook endpoint with auth, parse, and logging"
```

---

### Task 5: Local end-to-end verification (M0 done line)

**Files:**
- Create: `README.md` (run instructions)

**Interfaces:**
- Consumes: everything above.
- Produces: a documented, manually verified running server.

- [ ] **Step 1: Write run instructions**

`README.md`:
```markdown
# trading-bot

TradingView webhook → execution bot for BTC futures. See
`BTC-Futures-TradingBot-Design-V1.md` for the design and
`docs/superpowers/plans/` for implementation plans.

## Run locally (M0)

    python3 -m venv .venv && . .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env        # then edit WEBHOOK_TOKEN
    export $(grep -v '^#' .env | xargs)
    uvicorn tradingbot.app:create_app --factory --app-dir src --reload

Health check:

    curl localhost:8000/health

Send a test signal (replace TOKEN):

    curl -X POST localhost:8000/webhook \
      -H 'Content-Type: application/json' \
      -d '{"token":"TOKEN","strategy":"btc-futures-v1","action":"buy",
           "symbol":"BTCUSDT","order_type":"market","quantity":0.01,
           "position_side":"long"}'

## Test

    pytest -v

## Expose to TradingView (manual test)

Use a tunnel to get a public HTTPS URL, then set it as the alert webhook URL:

    # e.g. cloudflared tunnel --url http://localhost:8000
    # or:  ngrok http 8000
```

- [ ] **Step 2: Start the server and verify /health**

Run:
```bash
. .venv/bin/activate
export $(grep -v '^#' .env | xargs)
uvicorn tradingbot.app:create_app --factory --app-dir src &
sleep 2
curl -s localhost:8000/health
```
Expected: `{"status":"ok","venue":"bybit_testnet"}`

- [ ] **Step 3: Verify a valid signal is accepted and logged**

Run (use the token from your `.env`):
```bash
curl -s -X POST localhost:8000/webhook -H 'Content-Type: application/json' \
  -d '{"token":"YOUR_TOKEN","strategy":"btc-futures-v1","action":"buy","symbol":"BTCUSDT","order_type":"market","quantity":0.01,"position_side":"long"}'
```
Expected: `{"status":"received","action":"buy","symbol":"BTCUSDT"}` and a
`Signal received: action=buy ...` line in the server log (with NO token in it).

- [ ] **Step 4: Verify a bad token is rejected**

Run:
```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST localhost:8000/webhook \
  -H 'Content-Type: application/json' \
  -d '{"token":"wrong","strategy":"x","action":"buy","symbol":"BTCUSDT","quantity":0.01,"position_side":"long"}'
```
Expected: `401`

- [ ] **Step 5: Stop the server and commit docs**

```bash
kill %1 2>/dev/null || true
git add README.md
git commit -m "docs: add M0 run and verification instructions"
```

- [ ] **Step 6 (optional): Real TradingView round-trip**

Start a tunnel (`cloudflared tunnel --url http://localhost:8000` or `ngrok http 8000`),
put the HTTPS URL + `/webhook` as the alert's webhook URL, set the alert message to
the JSON payload (with your token), trigger the alert, and confirm the
`Signal received` log line. This closes M0.

---

## M0 → M1 handoff (why the skeleton fits)

M1 adds `venues/base.py` (the `ExecutionVenue` Protocol + `Order`/`OrderResult`/
`Position`), `venues/bybit.py` (`BybitTestnetVenue`), and `router.py`
(`Router.handle(signal)` mapping a `Signal` to `place_order`/`close_position`).
The only change to M0 code is one line in `app.py`: the `logger.info(...)`
placeholder becomes `router.handle(signal)`. Config already carries `venue`, so
selecting the venue is a lookup, not a rewrite.

---

## Self-Review

**Spec coverage (M0 scope of `BTC-Futures-TradingBot-Design-V1.md`):**
- §4 webhook endpoint → Task 4. SignalParser → Task 2. ExecutionVenue seam →
  reserved layout + handoff (built in M1, correctly out of M0 scope). Router seam
  → documented placeholder in Task 4 / handoff.
- §5 data contracts (payload, `Signal`) → Tasks 2 & 4.
- §6 security: token-in-body constant-time → Tasks 3 & 4; IP allowlist → Tasks 3
  & 4; `.env`/fail-fast config → Task 1; HTTPS → M2 (reverse proxy, out of M0),
  noted in README tunnel step for manual testing.
- §7 M0 milestone (receive, validate, log, verify via curl + TradingView tunnel)
  → Tasks 4 & 5.
- §8 testing: parser good/bad → Task 2; auth rejects bad token/IP → Tasks 3 & 4.
  (Router `FakeVenue` and Bybit integration tests are M1.)

**Placeholder scan:** No TBD/TODO. The `logger.info` in Task 4 is an intentional,
documented M0 deliverable (log-only), not a stub — replaced in M1 per handoff.

**Type consistency:** `Config(webhook_token, venue, allowed_ips)`, `Signal`
fields, `parse_signal`/`SignalParseError`, `is_authorized`/`ip_allowed`, and
`create_app(config)` names/signatures are identical across Tasks 1–5.
