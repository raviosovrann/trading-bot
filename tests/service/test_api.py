"""Tests for the REST API and WebSocket endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tradingbot.models import Action, Candle, Order, OrderResult, OrderType, Position, PositionSide, Signal
from tradingbot.service.api import create_app
from tradingbot.service.auth import hash_password
from tradingbot.service.events import (
    BotStateEvent,
    DecisionEvent,
    EventBus,
    OrderEvent,
    OverflowEvent,
)
from tradingbot.service.risk import GlobalExposure
from tradingbot.service.store import BotStore
from tradingbot.stream import StreamingNotSupported
from tradingbot.service.supervisor import BotConfig, BotSupervisor

_TOKEN = "test-token"
_TOKEN_HASH = hashlib.sha256(_TOKEN.encode()).hexdigest()
_USERNAME = "test"
_PASSWORD = "s3cret-pass"


def _wait_for_subscribers(bus: EventBus, minimum: int = 1, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while bus.subscriber_count() < minimum and time.time() < deadline:
        time.sleep(0.01)
    assert bus.subscriber_count() >= minimum, "subscriber not registered"


class _FakeHub:
    def __init__(self) -> None:
        self.handlers: dict[tuple[str, str], list] = {}
        self.warmups = 0

    async def warmup(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        del symbol, timeframe, limit
        self.warmups += 1
        return [_candle()]

    def subscribe(self, symbol: str, timeframe: str, handler) -> None:
        self.handlers.setdefault((symbol, timeframe), []).append(handler)

    def unsubscribe(self, symbol: str, timeframe: str, handler) -> None:
        self.handlers[(symbol, timeframe)].remove(handler)

    def latest_price(self, symbol: str, timeframe: str) -> float | None:
        del symbol, timeframe
        return 100.0


class _FakeVenue:
    def place_order(self, order: Order) -> OrderResult:
        return OrderResult(
            ok=True,
            order_id="order-1",
            status="filled",
            filled_qty=order.qty,
            raw={},
        )

    def close_position(self, symbol: str) -> OrderResult:
        del symbol
        return OrderResult(ok=True, order_id=None, status="no position", filled_qty=0.0, raw={})

    def get_position(self, symbol: str) -> Position | None:
        del symbol
        return None

    def health_check(self) -> bool:
        return True


class _SignalStrategy:
    def on_bar(self, candles) -> Signal | None:
        del candles
        return Signal(
            strategy="test",
            action=Action.buy,
            symbol="BTC/USD",
            order_type=OrderType.market,
            quantity=0.1,
            position_side=PositionSide.long,
        )


def _candle(ts: int = 1, close: float = 100.0) -> Candle:
    return Candle(timestamp=ts, open=close, high=close, low=close, close=close, volume=1.0)


def _store(tmp_path: Path) -> BotStore:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "users.json").write_text(
        json.dumps({
            "users": [{
                "username": _USERNAME,
                "token_hash": _TOKEN_HASH,
                "password_hash": hash_password(_PASSWORD),
            }]
        })
    )
    (data_dir / "trades").mkdir()
    store = BotStore(data_dir)
    # Secrets are encrypted at rest; write them via the store so the on-disk
    # file is a Fernet token, not clear text.
    store.save_secrets("coinbase", "spot", {"api_key": "secret-key", "api_secret": "secret-secret"})
    return store


def _supervisor(monkeypatch: pytest.MonkeyPatch) -> BotSupervisor:
    monkeypatch.setattr("tradingbot.service.supervisor.build_venue", lambda *a, **k: _FakeVenue())
    monkeypatch.setattr("tradingbot.service.supervisor.build_strategy", lambda *a, **k: _SignalStrategy())
    return BotSupervisor(
        hub_factory=lambda cfg: _FakeHub(),
        event_bus=EventBus(),
        global_exposure=GlobalExposure(),
    )


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    store = _store(tmp_path)
    supervisor = _supervisor(monkeypatch)
    app = create_app(store=store, supervisor=supervisor)
    with TestClient(app) as test_client:
        yield test_client


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


def _login(client: TestClient) -> str:
    """Log in via password so the client's cookie jar holds a session.

    Returns the CSRF token, which cookie-authenticated state-changing requests
    must echo in the ``X-CSRF-Token`` header.
    """
    response = client.post("/api/login", json={"username": _USERNAME, "password": _PASSWORD})
    assert response.status_code == 200
    return client.cookies["tb_csrf"]


def _csrf(client: TestClient) -> dict[str, str]:
    """Return the CSRF header for the client's current session."""
    return {"X-CSRF-Token": client.cookies["tb_csrf"]}


class TestSpaServing:
    def _client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        dist = tmp_path / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<!doctype html><title>SPA</title>", encoding="utf-8")
        (dist / "assets" / "app.js").write_text("console.log('hi')", encoding="utf-8")
        app = create_app(store=_store(tmp_path), supervisor=_supervisor(monkeypatch), spa_dir=dist)
        return TestClient(app)

    def test_root_serves_index(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The SPA index is served at / when a dist is provided."""
        client = self._client(tmp_path, monkeypatch)
        response = client.get("/")
        assert response.status_code == 200
        assert "SPA" in response.text

    def test_static_asset_is_served(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A legitimate file under the mounted assets directory remains available."""
        client = self._client(tmp_path, monkeypatch)
        response = client.get("/assets/app.js")
        assert response.status_code == 200
        assert response.text == "console.log('hi')"

    def test_deep_link_falls_back_to_index(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A client-side route falls back to index.html (SPA routing)."""
        client = self._client(tmp_path, monkeypatch)
        response = client.get("/bots/some-id")
        assert response.status_code == 200
        assert "SPA" in response.text

    @pytest.mark.parametrize(
        "path",
        [
            "/%2e%2e%2Foutside.txt",
            "/..%2Foutside.txt",
            "/nested%2F..%2F..%2Foutside.txt",
            "/%2e%2e/%2e%2e%2Foutside.txt",
        ],
    )
    def test_encoded_path_traversal_cannot_escape_dist(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        path: str,
    ) -> None:
        """Percent-decoded traversal never serves a file outside the SPA root."""
        secret = "must-not-leak"
        (tmp_path / "outside.txt").write_text(secret, encoding="utf-8")
        client = self._client(tmp_path, monkeypatch)

        response = client.get(path)

        assert response.status_code == 200
        assert "SPA" in response.text
        assert secret not in response.text

    def test_symlink_cannot_escape_dist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bundle symlink cannot expose a file outside the SPA root."""
        secret = "must-not-leak"
        outside = tmp_path / "outside.txt"
        outside.write_text(secret, encoding="utf-8")
        client = self._client(tmp_path, monkeypatch)
        (tmp_path / "dist" / "escape.txt").symlink_to(outside)

        response = client.get("/escape.txt")

        assert response.status_code == 200
        assert "SPA" in response.text
        assert secret not in response.text

    def test_api_not_shadowed_by_spa(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """API routes still resolve when the SPA catch-all is mounted."""
        client = self._client(tmp_path, monkeypatch)
        assert client.get("/api/bots").status_code == 401  # auth still enforced, not index.html
        assert client.get("/api/venues", headers=_auth()).status_code == 200


class TestAuth:
    def test_unauthenticated_request_returns_401(self, client: TestClient) -> None:
        """Verify that unauthenticated requests return 401."""
        response = client.get("/api/bots")
        assert response.status_code == 401


class TestLogin:
    def test_valid_credentials_open_a_cookie_session(self, client: TestClient) -> None:
        """Login sets an HttpOnly session cookie (no token) that authenticates."""
        response = client.post("/api/login", json={"username": _USERNAME, "password": _PASSWORD})
        assert response.status_code == 200
        # No secret is returned in the body — only the user's display info.
        body = response.json()
        assert body["username"] == _USERNAME and "token" not in body
        # The session cookie is HttpOnly and now authenticates protected reads.
        set_cookie = response.headers["set-cookie"]
        assert "tb_session=" in set_cookie and "httponly" in set_cookie.lower()
        assert client.get("/api/bots").status_code == 200

    def test_no_auth_secret_is_readable_by_javascript(self, client: TestClient) -> None:
        """The session cookie is HttpOnly; only the CSRF companion is readable."""
        response = client.post(
            "/api/login", json={"username": _USERNAME, "password": _PASSWORD}
        )
        cookies = response.headers.get_list("set-cookie")
        session_cookie = next(c for c in cookies if c.startswith("tb_session="))
        csrf_cookie = next(c for c in cookies if c.startswith("tb_csrf="))
        assert "httponly" in session_cookie.lower()
        assert "httponly" not in csrf_cookie.lower()

    def test_wrong_password_is_rejected(self, client: TestClient) -> None:
        """Login with a bad password returns 401 and sets no session."""
        response = client.post("/api/login", json={"username": _USERNAME, "password": "wrong"})
        assert response.status_code == 401
        assert "set-cookie" not in response.headers

    def test_unknown_user_is_rejected(self, client: TestClient) -> None:
        """Login with an unknown username returns 401."""
        response = client.post("/api/login", json={"username": "nobody", "password": _PASSWORD})
        assert response.status_code == 401

    def test_disabled_user_cannot_log_in(self, client: TestClient) -> None:
        """A disabled account is rejected even with the correct password."""
        store = client.app.state.store  # type: ignore[attr-defined]
        store.update_user(_USERNAME, updates={"disabled": True})
        response = client.post("/api/login", json={"username": _USERNAME, "password": _PASSWORD})
        assert response.status_code == 401

    def test_login_upgrades_a_stale_password_hash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A weak-iteration stored hash is upgraded on successful login."""
        from tradingbot.service.auth import hash_password as hp, needs_rehash

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "users.json").write_text(
            json.dumps({"users": [{"username": _USERNAME, "password_hash": hp(_PASSWORD, iterations=1000)}]})
        )
        (data_dir / "trades").mkdir()
        store = BotStore(data_dir)
        app = create_app(store=store, supervisor=_supervisor(monkeypatch))
        with TestClient(app) as client:
            assert client.post(
                "/api/login", json={"username": _USERNAME, "password": _PASSWORD}
            ).status_code == 200
        upgraded = store.load_users()["users"][0]["password_hash"]
        assert needs_rehash(upgraded) is False

    def test_unknown_user_still_runs_real_pbkdf2(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unknown username verifies against a real PBKDF2 hash, not the empty
        short-circuit, so login timing does not leak which usernames exist."""
        import tradingbot.service.api as api_mod

        seen: list[str] = []
        original = api_mod.verify_password
        monkeypatch.setattr(
            api_mod, "verify_password", lambda p, h: seen.append(h) or original(p, h)
        )
        client.post("/api/login", json={"username": "nobody", "password": "x"})
        assert seen and seen[0].startswith("pbkdf2_sha256$")


class TestLoginThrottle:
    def test_repeated_failures_lock_out_with_429(self, client: TestClient) -> None:
        """After the failure threshold, further attempts are throttled with 429."""
        for _ in range(5):
            resp = client.post("/api/login", json={"username": _USERNAME, "password": "wrong"})
            assert resp.status_code == 401
        locked = client.post("/api/login", json={"username": _USERNAME, "password": "wrong"})
        assert locked.status_code == 429
        assert "retry-after" in {k.lower() for k in locked.headers}
        # Even the correct password is refused while locked out.
        assert client.post(
            "/api/login", json={"username": _USERNAME, "password": _PASSWORD}
        ).status_code == 429

    def test_successful_login_resets_failure_count(self, client: TestClient) -> None:
        """A success clears the counter so a later typo does not compound."""
        for _ in range(4):
            client.post("/api/login", json={"username": _USERNAME, "password": "wrong"})
        assert client.post(
            "/api/login", json={"username": _USERNAME, "password": _PASSWORD}
        ).status_code == 200
        # Counter reset: another wrong attempt is a plain 401, not a lockout.
        assert client.post(
            "/api/login", json={"username": _USERNAME, "password": "wrong"}
        ).status_code == 401


class TestNoSecretLeak:
    def test_login_does_not_log_password_or_session(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Neither the password nor the session id/CSRF appears in logs."""
        with caplog.at_level("DEBUG"):
            response = client.post(
                "/api/login", json={"username": _USERNAME, "password": _PASSWORD}
            )
        assert response.status_code == 200
        set_cookie = response.headers["set-cookie"]
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert _PASSWORD not in logged
        assert "tb_session" not in logged
        # The raw session/csrf values from the Set-Cookie never reach the logs.
        for chunk in set_cookie.replace("=", " ").split():
            if len(chunk) > 20:  # cookie value-sized tokens
                assert chunk not in logged


class TestSessionLifecycle:
    def test_session_endpoint_returns_user_when_logged_in(self, client: TestClient) -> None:
        """GET /api/session restores SPA state without exposing a secret."""
        _login(client)
        response = client.get("/api/session")
        assert response.status_code == 200
        assert response.json()["username"] == _USERNAME

    def test_session_endpoint_401_when_logged_out(self, client: TestClient) -> None:
        """With no session, /api/session is 401 so the SPA shows the login page."""
        assert client.get("/api/session").status_code == 401

    def test_logout_revokes_session_and_blocks_further_calls(self, client: TestClient) -> None:
        """Logout revokes the session so subsequent REST calls are rejected."""
        _login(client)
        assert client.get("/api/bots").status_code == 200
        assert client.post("/api/logout", headers=_csrf(client)).status_code == 204
        # The cookie jar no longer resolves to a live session.
        assert client.get("/api/session").status_code == 401

    def test_logout_without_csrf_is_rejected(self, client: TestClient) -> None:
        """Logout is a state change, so a cookie session must present CSRF."""
        _login(client)
        assert client.post("/api/logout").status_code == 403

    def test_expired_session_is_rejected(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A session past its idle window no longer authenticates."""
        _login(client)
        # Advance the session store's clock beyond the absolute lifetime.
        store_sessions = client.app.state.sessions  # type: ignore[attr-defined]
        base = time.time()
        monkeypatch.setattr(store_sessions, "_clock", lambda: base + 13 * 60 * 60)
        assert client.get("/api/session").status_code == 401


class TestCsrf:
    def test_cookie_state_change_requires_csrf_header(self, client: TestClient) -> None:
        """A cookie-authenticated mutation without the CSRF header is 403."""
        _login(client)
        response = client.put(
            "/api/venues/kraken/spot/secrets",
            json={"api_key": "k", "api_secret": "s"},
        )
        assert response.status_code == 403

    def test_cookie_state_change_succeeds_with_csrf_header(self, client: TestClient) -> None:
        """The same mutation succeeds when the CSRF token is echoed back."""
        _login(client)
        response = client.put(
            "/api/venues/kraken/spot/secrets",
            json={"api_key": "k", "api_secret": "s"},
            headers=_csrf(client),
        )
        assert response.status_code == 204

    def test_bearer_api_caller_is_csrf_exempt(self, client: TestClient) -> None:
        """Direct-API bearer callers carry no ambient cookie and skip CSRF."""
        response = client.put(
            "/api/venues/kraken/spot/secrets",
            json={"api_key": "k", "api_secret": "s"},
            headers=_auth(),
        )
        assert response.status_code == 204


class TestProbes:
    def test_healthz_is_unauthenticated_and_ok(self, client: TestClient) -> None:
        """Liveness is reachable without credentials."""
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_readyz_reports_ready_when_dependencies_are_healthy(
        self, client: TestClient
    ) -> None:
        """Readiness is 200 with per-dependency detail when the store is usable."""
        response = client.get("/readyz")
        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is True
        assert set(body["checks"]) == {"storage", "secrets_key"}

    def test_readyz_is_503_when_secrets_key_is_missing(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unusable dependency makes readiness fail closed with 503."""
        monkeypatch.delenv("TRADINGBOT_SECRETS_KEY", raising=False)
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["ready"] is False


class TestAuditTrail:
    def _admin_client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        """A client whose operator has the admin role (to read the audit log)."""
        store = _store(tmp_path)
        store.update_user(_USERNAME, updates={"roles": ["admin"]})
        app = create_app(store=store, supervisor=_supervisor(monkeypatch))
        return TestClient(app)

    def test_login_and_mutations_are_audited(self, client: TestClient) -> None:
        """A login and a secrets write both land in the audit log, attributed."""
        _login(client)
        client.put(
            "/api/venues/kraken/spot/secrets",
            json={"api_key": "k", "api_secret": "s"},
            headers=_csrf(client),
        )
        events, _ = client.app.state.store.read_audit()  # type: ignore[attr-defined]
        actions = {e["action"] for e in events}
        assert {"login", "credentials.update"} <= actions
        cred_event = next(e for e in events if e["action"] == "credentials.update")
        assert cred_event["actor_name"] == _USERNAME
        assert cred_event["outcome"] == "success"

    def test_audit_never_stores_secret_values(self, client: TestClient) -> None:
        """Credential values never reach the audit log — only key names."""
        _login(client)
        client.put(
            "/api/venues/kraken/spot/secrets",
            json={"api_key": "super-secret-key"},
            headers=_csrf(client),
        )
        raw = (Path(client.app.state.store._data_dir) / "audit.jsonl").read_text()  # type: ignore[attr-defined]
        assert "super-secret-key" not in raw

    def test_failed_login_is_audited(self, client: TestClient) -> None:
        """A failed login is recorded with a failure outcome and no principal."""
        client.post("/api/login", json={"username": _USERNAME, "password": "wrong"})
        events, _ = client.app.state.store.read_audit()  # type: ignore[attr-defined]
        failure = next(e for e in events if e["outcome"] == "failure")
        assert failure["action"] == "login"
        assert failure["actor_name"] == "anonymous"

    def test_audit_endpoint_requires_admin(self, client: TestClient) -> None:
        """A plain operator cannot read the audit log."""
        _login(client)
        assert client.get("/api/audit").status_code == 403

    def test_admin_can_read_audit_with_chain_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An admin reads the paginated log and sees an intact chain."""
        client = self._admin_client(tmp_path, monkeypatch)
        client.post("/api/login", json={"username": _USERNAME, "password": _PASSWORD})
        response = client.get("/api/audit")
        assert response.status_code == 200
        body = response.json()
        assert body["chain_ok"] is True
        assert any(e["action"] == "login" for e in body["events"])

    def test_response_carries_request_id(self, client: TestClient) -> None:
        """The request-id middleware echoes a correlation id on every response."""
        response = client.get("/api/session")
        assert response.headers.get("X-Request-ID")


class TestSecrets:
    def test_put_secrets_persists_and_returns_no_body(self, client: TestClient) -> None:
        """Storing secrets returns 204, persists them, and echoes nothing back."""
        response = client.put(
            "/api/venues/kraken/spot/secrets",
            json={"api_key": "new-key", "api_secret": "new-secret"},
            headers=_auth(),
        )
        assert response.status_code == 204
        assert response.content == b""
        stored = client.app.state.store.load_secrets()["kraken"]["spot"]  # type: ignore[attr-defined]
        assert stored == {"api_key": "new-key", "api_secret": "new-secret"}

    def test_put_secrets_requires_auth(self, client: TestClient) -> None:
        """Storing secrets without a token is rejected."""
        response = client.put("/api/venues/kraken/spot/secrets", json={"api_key": "k"})
        assert response.status_code == 401

    def test_put_empty_secrets_is_rejected(self, client: TestClient) -> None:
        """An empty credential payload is a 400, not a stored empty record."""
        response = client.put("/api/venues/kraken/spot/secrets", json={}, headers=_auth())
        assert response.status_code == 400


class TestListMeta:
    def test_venues_and_strategies_are_non_empty(self, client: TestClient) -> None:
        """Verify that venues and strategies endpoints return non-empty lists."""
        venues = client.get("/api/venues", headers=_auth()).json()
        strategies = client.get("/api/strategies", headers=_auth()).json()
        assert any(v["venue"] == "coinbase" and v["market_type"] == "spot" for v in venues)
        assert "example" in strategies


class TestBotLifecycle:
    def _create(self, client: TestClient, **overrides: object) -> dict:
        payload = {
            "venue": "coinbase",
            "market_type": "spot",
            "strategy": "example",
            "symbol": "BTC/USD",
            "timeframe": "1m",
            "quantity": 0.1,
            "per_bot_cap": 1_000.0,
            "global_cap": 10_000.0,
            "params": {},
        }
        payload.update(overrides)
        response = client.post("/api/bots", json=payload, headers=_auth())
        assert response.status_code == 201
        return response.json()

    def test_create_bot_dry_run_default(self, client: TestClient) -> None:
        """Verify that a newly created bot defaults to dry-run and created status."""
        bot = self._create(client)
        assert bot["live"] is False
        assert bot["status"] == "created"

    def test_start_bot_then_get_shows_running(self, client: TestClient) -> None:
        """Verify that starting a bot sets its status to running and stopping sets it to stopped."""
        bot = self._create(client)
        bot_id = bot["id"]
        response = client.post(f"/api/bots/{bot_id}/start", headers=_auth())
        assert response.status_code == 200
        assert response.json()["status"] == "running"

        response = client.get(f"/api/bots/{bot_id}", headers=_auth())
        assert response.status_code == 200
        assert response.json()["status"] == "running"

        response = client.post(f"/api/bots/{bot_id}/stop", headers=_auth())
        assert response.status_code == 200
        assert response.json()["status"] == "stopped"

    def test_patch_bot_flips_live(self, client: TestClient) -> None:
        """Verify that patching a bot flips the live flag."""
        bot = self._create(client)
        bot_id = bot["id"]
        response = client.patch(f"/api/bots/{bot_id}", json={"live": True}, headers=_auth())
        assert response.status_code == 200
        assert response.json()["live"] is True

        response = client.get(f"/api/bots/{bot_id}", headers=_auth())
        assert response.json()["live"] is True

    def test_bot_response_hides_secrets(self, client: TestClient) -> None:
        """Verify that bot responses do not expose credential fields."""
        bot = self._create(client)
        bot_id = bot["id"]
        response = client.get(f"/api/bots/{bot_id}", headers=_auth())
        body = response.text
        assert "secret-key" not in body
        assert "secret-secret" not in body
        assert "api_key" not in body
        assert "api_secret" not in body

    def test_list_bots_after_create(self, client: TestClient) -> None:
        """Verify that the created bot appears in the list."""
        bot = self._create(client)
        response = client.get("/api/bots", headers=_auth())
        assert response.status_code == 200
        assert any(b["id"] == bot["id"] for b in response.json())

    def test_get_unknown_bot_returns_404(self, client: TestClient) -> None:
        """Verify that fetching an unknown bot returns 404."""
        response = client.get("/api/bots/no-such-bot", headers=_auth())
        assert response.status_code == 404

    def test_start_unknown_bot_returns_404(self, client: TestClient) -> None:
        """Verify that starting an unknown bot returns 404."""
        response = client.post("/api/bots/no-such-bot/start", headers=_auth())
        assert response.status_code == 404

    def test_start_bot_returns_400_when_venue_build_fails(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify that starting a bot returns 400 when the venue cannot be built."""
        def _raise(*a: object, **k: object) -> None:
            raise ValueError("bad creds")

        monkeypatch.setattr("tradingbot.service.supervisor.build_venue", _raise)
        bot = self._create(client)
        response = client.post(f"/api/bots/{bot['id']}/start", headers=_auth())
        assert response.status_code == 400
        assert "bad creds" in response.json()["detail"]


class TestWebSocket:
    def test_ws_receives_published_order_event(self, client: TestClient) -> None:
        """Verify that the WebSocket receives published order events."""
        app = cast(FastAPI, client.app)
        supervisor = app.state.supervisor
        # Authenticate the WS via the session cookie — no token in the URL.
        _login(client)
        with client.websocket_connect("/ws") as ws:
            _wait_for_subscribers(supervisor.event_bus)
            supervisor.event_bus.publish(
                OrderEvent(bot_id="b1", action="buy", status="filled", ok=True, order_id="1")
            )
            data = ws.receive_json()
            assert data["type"] == "order"
            assert data["bot_id"] == "b1"
            assert data["action"] == "buy"

    def test_ws_without_session_is_closed(self, client: TestClient) -> None:
        """A WebSocket connection without a valid session cookie is rejected."""
        with pytest.raises(Exception):
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()

    def test_ws_rejects_disallowed_origin(self, client: TestClient) -> None:
        """A cross-origin upgrade is rejected even with a valid session cookie."""
        _login(client)
        with pytest.raises(Exception):
            with client.websocket_connect(
                "/ws", headers={"origin": "https://evil.example"}
            ) as ws:
                ws.receive_json()

    def test_ws_no_token_appears_in_url(self, client: TestClient) -> None:
        """The client never needs a token in the WS URL to authenticate."""
        _login(client)
        with client.websocket_connect("/ws") as ws:
            # Connection succeeds purely from the cookie; the URL carries no secret.
            assert ws is not None


class TestTrades:
    def test_get_trades_for_unknown_bot_returns_404(self, client: TestClient) -> None:
        """Verify that fetching trades for an unknown bot returns 404."""
        response = client.get("/api/bots/no-such-bot/trades", headers=_auth())
        assert response.status_code == 404

    def test_get_trades_for_bot(self, client: TestClient) -> None:
        """Verify that trades for a bot are returned as typed TradeView records."""
        bot = TestBotLifecycle()._create(client)
        bot_id = bot["id"]
        store = cast(FastAPI, client.app).state.store
        store.append_trade(bot_id, {
            "bot_id": bot_id, "action": "buy", "status": "submitted",
            "ok": True, "order_id": "o1", "symbol": "BTC/USD", "ts": 42,
        })
        response = client.get(f"/api/bots/{bot_id}/trades", headers=_auth())
        assert response.status_code == 200
        # A row with no `kind` predates #135. The ledger fields come back null
        # rather than guessed at: the data needed to classify it honestly was
        # never recorded, so inventing it would recreate the bug #135 fixes.
        assert response.json() == {
            "items": [{
                "bot_id": bot_id, "action": "buy", "status": "submitted",
                "ok": True, "order_id": "o1", "symbol": "BTC/USD", "ts": 42, "seq": 1,
                "kind": None, "client_order_id": None, "side": None, "qty": None,
                "filled_qty": None, "avg_price": None, "reason": None,
            }],
            "next_cursor": None,
        }

    def test_get_trades_exposes_ledger_events(self, client: TestClient) -> None:
        """A #135 lifecycle event is surfaced with its kind and quantities."""
        bot = TestBotLifecycle()._create(client)
        bot_id = bot["id"]
        store = cast(FastAPI, client.app).state.store
        store.append_trade(bot_id, {
            "kind": "submitted", "bot_id": bot_id, "client_order_id": "c1",
            "symbol": "BTC/USD", "side": "buy", "order_type": "market",
            "qty": 2.0, "price": None, "venue_order_id": "v1", "ts": 42,
        })
        store.append_trade(bot_id, {
            "kind": "order_status", "client_order_id": "c1",
            "filled_qty": 2.0, "avg_price": 150.0, "ts": 43,
        })

        response = client.get(f"/api/bots/{bot_id}/trades", headers=_auth())
        assert response.status_code == 200
        items = response.json()["items"]

        assert [item["kind"] for item in items] == ["order_status", "submitted"]
        snapshot, submitted = items
        assert snapshot["filled_qty"] == 2.0
        assert snapshot["avg_price"] == 150.0
        assert submitted["side"] == "buy"
        assert submitted["qty"] == 2.0
        # The submission carries no fill evidence, which is the whole point.
        assert submitted["filled_qty"] is None
        # venue_order_id surfaces through the existing order_id field.
        assert submitted["order_id"] == "v1"

    def test_get_trades_tolerates_partial_records(self, client: TestClient) -> None:
        """A legacy/partial trade record is coerced, not 500'd."""
        bot = TestBotLifecycle()._create(client)
        bot_id = bot["id"]
        store = cast(FastAPI, client.app).state.store
        store.append_trade(bot_id, {"action": "sell", "status": "filled"})
        response = client.get(f"/api/bots/{bot_id}/trades", headers=_auth())
        assert response.status_code == 200
        row = response.json()["items"][0]
        assert row["action"] == "sell" and row["ok"] is False and row["order_id"] is None


class TestCredentialRotation:
    def _bot(self, client: TestClient) -> str:
        return TestBotLifecycle()._create(client)["id"]

    def test_rotation_is_refused_while_a_bot_on_that_account_runs(
        self, client: TestClient
    ) -> None:
        """Rotating under a running bot cannot half-apply, so it is refused.

        Same policy as #109: the venue client is built once at start, so a
        rotation mid-flight would leave the API advertising credentials the
        running bot is not using.
        """
        bot_id = self._bot(client)
        _login(client)
        client.post(f"/api/bots/{bot_id}/start", headers=_csrf(client))

        response = client.put(
            "/api/venues/coinbase/spot/secrets",
            json={"api_key": "new", "api_secret": "new"},
            headers=_csrf(client),
        )

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert bot_id in detail, "the operator needs to know which bot blocks it"

    def test_rotation_succeeds_once_the_bot_is_stopped(self, client: TestClient) -> None:
        """Verify the documented stop -> rotate -> start flow works."""
        bot_id = self._bot(client)
        _login(client)
        client.post(f"/api/bots/{bot_id}/start", headers=_csrf(client))
        client.post(f"/api/bots/{bot_id}/stop", headers=_csrf(client))

        response = client.put(
            "/api/venues/coinbase/spot/secrets",
            json={"api_key": "new", "api_secret": "new"},
            headers=_csrf(client),
        )

        assert response.status_code == 204
        store = cast(FastAPI, client.app).state.store
        assert store.load_secrets()["coinbase"]["spot"]["api_key"] == "new"

    def test_a_bot_on_another_account_does_not_block_rotation(
        self, client: TestClient
    ) -> None:
        """Verify only the affected venue/market blocks its own rotation."""
        bot_id = self._bot(client)
        _login(client)
        client.post(f"/api/bots/{bot_id}/start", headers=_csrf(client))

        response = client.put(
            "/api/venues/tradovate/futures/secrets",
            json={"api_key": "t", "api_secret": "t"},
            headers=_csrf(client),
        )

        assert response.status_code == 204

    def test_rotation_invalidates_the_cached_clients(self, client: TestClient) -> None:
        """Verify the endpoint eagerly drops superseded clients.

        Lazily rebuilding on the next start would leave the old socket
        reconnecting on a key the operator believes they revoked.
        """
        app = cast(FastAPI, client.app)
        invalidated: list[tuple[str, str]] = []

        class _Factory:
            def __call__(self, cfg):  # pragma: no cover - not exercised here
                raise AssertionError("not used in this test")

            def invalidate(self, venue: str, market_type: str) -> None:
                invalidated.append((venue, market_type))

        app.state.supervisor._hub_factory = _Factory()
        _login(client)

        response = client.put(
            "/api/venues/coinbase/spot/secrets",
            json={"api_key": "new", "api_secret": "new"},
            headers=_csrf(client),
        )

        assert response.status_code == 204
        assert invalidated == [("coinbase", "spot")]


class TestDeleteBot:
    """DELETE /api/bots/{id} (#163)."""

    def _bot(self, client: TestClient) -> str:
        return TestBotLifecycle()._create(client)["id"]

    def test_a_stopped_bot_is_deleted(self, client: TestClient) -> None:
        """Verify the bot disappears from the API."""
        bot_id = self._bot(client)
        _login(client)

        response = client.delete(f"/api/bots/{bot_id}", headers=_csrf(client))

        assert response.status_code == 204
        assert client.get(f"/api/bots/{bot_id}", headers=_auth()).status_code == 404
        listed = client.get("/api/bots", headers=_auth()).json()
        assert all(b["id"] != bot_id for b in listed)

    def test_deleting_a_running_bot_is_refused(self, client: TestClient) -> None:
        """Verify a running bot cannot be deleted, and nothing changes."""
        bot_id = self._bot(client)
        _login(client)
        client.post(f"/api/bots/{bot_id}/start", headers=_csrf(client))

        response = client.delete(f"/api/bots/{bot_id}", headers=_csrf(client))

        assert response.status_code == 409
        assert "running" in response.json()["detail"]
        assert client.get(f"/api/bots/{bot_id}", headers=_auth()).status_code == 200

    def test_deleting_an_unknown_bot_returns_404(self, client: TestClient) -> None:
        """Verify an unknown id is a 404, not a 500."""
        _login(client)
        response = client.delete("/api/bots/no-such-bot", headers=_csrf(client))
        assert response.status_code == 404

    def test_delete_requires_csrf(self, client: TestClient) -> None:
        """Verify deletion is CSRF-protected like every other state change."""
        bot_id = self._bot(client)
        _login(client)
        response = client.delete(f"/api/bots/{bot_id}")
        assert response.status_code == 403

    def test_deletion_is_audited(self, client: TestClient) -> None:
        """Verify the audit trail records what was removed."""
        bot_id = self._bot(client)
        _login(client)
        client.delete(f"/api/bots/{bot_id}", headers=_csrf(client))

        app = cast(FastAPI, client.app)
        records, _ = app.state.store.read_audit(limit=50)
        deletes = [r for r in records if r.get("action") == "bot.delete"]
        assert deletes, "deletion must be audited"
        assert deletes[0]["target"] == f"bot:{bot_id}"
        assert deletes[0]["outcome"] == "success"

    def test_a_deleted_bot_does_not_return_after_a_restart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify the config is removed from disk, not just from memory.

        Since #108 the supervisor restores persisted bots on startup, so an
        in-memory-only delete would quietly resurrect the bot.
        """
        store = _store(tmp_path)
        app = create_app(store=store, supervisor=_supervisor_with_store(monkeypatch, store))
        with TestClient(app) as client:
            bot_id = client.post("/api/bots", json=_BOT_PAYLOAD, headers=_auth()).json()["id"]
            _login(client)
            assert client.delete(f"/api/bots/{bot_id}", headers=_csrf(client)).status_code == 204

        restarted_store = BotStore(store.data_dir)
        restarted = create_app(
            store=restarted_store,
            supervisor=_supervisor_with_store(monkeypatch, restarted_store),
        )
        with TestClient(restarted) as client:
            assert client.get("/api/bots", headers=_auth()).json() == []

    def test_trade_history_is_archived_on_delete(self, client: TestClient) -> None:
        """Verify executed trades survive the bot that made them."""
        bot_id = self._bot(client)
        app = cast(FastAPI, client.app)
        store = app.state.store
        store.append_trade(bot_id, {"bot_id": bot_id, "action": "buy", "order_id": "o1"})
        _login(client)

        client.delete(f"/api/bots/{bot_id}", headers=_csrf(client))

        archive = Path(store.data_dir) / "trades" / "archive" / bot_id
        assert archive.is_dir()
        assert any(archive.glob("*.jsonl")), "history must be archived, not destroyed"


class TestVenueErrorSurfacing:
    """A venue failure must say whose fault it is, without leaking keys (#175)."""

    def _bot(self, client: TestClient) -> str:
        return TestBotLifecycle()._create(client)["id"]

    def test_an_unavailable_exchange_returns_502_not_500(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The geo-block that prompted this issue returned a bare 500."""
        import ccxt

        bot_id = self._bot(client)

        def _boom(*_a, **_k):
            raise ccxt.ExchangeNotAvailable(
                "binance GET https://api.binance.com/sapi/v1/capital/config/getall"
                "?timestamp=1784480879223&signature=f7adcc27fe7a49c2854d708dece4eb 451 "
                '{"msg": "Service unavailable from a restricted location"}'
            )

        monkeypatch.setattr("tradingbot.service.supervisor.build_venue", _boom)
        _login(client)
        response = client.post(f"/api/bots/{bot_id}/start", headers=_csrf(client))

        assert response.status_code == 502
        detail = response.json()["detail"]
        assert "restricted location" in detail
        assert "ExchangeNotAvailable" in detail

    def test_the_response_never_carries_credentials(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ccxt embeds the signed URL in its message; it must not reach the client.

        This is the one that matters: the signature and key would otherwise
        land in a browser, a proxy log and any error tracker in between.
        """
        import ccxt

        bot_id = self._bot(client)

        def _boom(*_a, **_k):
            raise ccxt.AuthenticationError(
                "coinbase GET https://api.coinbase.com/v2/accounts"
                "?api_key=LEAKED_KEY_VALUE&signature=LEAKED_SIGNATURE 401"
            )

        monkeypatch.setattr("tradingbot.service.supervisor.build_venue", _boom)
        _login(client)
        response = client.post(f"/api/bots/{bot_id}/start", headers=_csrf(client))

        body = response.text
        assert response.status_code == 400
        assert "LEAKED_KEY_VALUE" not in body
        assert "LEAKED_SIGNATURE" not in body
        assert "api_key=" not in body or "<redacted>" in body

    def test_rate_limiting_returns_429(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify a throttled venue is distinguishable from a broken one."""
        import ccxt

        bot_id = self._bot(client)
        monkeypatch.setattr(
            "tradingbot.service.supervisor.build_venue",
            lambda *a, **k: (_ for _ in ()).throw(ccxt.RateLimitExceeded("too many requests")),
        )
        _login(client)
        response = client.post(f"/api/bots/{bot_id}/start", headers=_csrf(client))

        assert response.status_code == 429

    def test_an_internal_bug_still_returns_500(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Our own faults must not be disguised as venue problems."""
        bot_id = self._bot(client)
        monkeypatch.setattr(
            "tradingbot.service.supervisor.build_venue",
            lambda *a, **k: (_ for _ in ()).throw(TypeError("bug in our own code")),
        )
        _login(client)
        with pytest.raises(TypeError):
            client.post(f"/api/bots/{bot_id}/start", headers=_csrf(client))


class TestUnsupportedVenue:
    def test_starting_on_a_venue_that_cannot_stream_returns_a_readable_error(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A venue that cannot stream must fail with a reason, not a bare 500.

        The operator has to learn the venue is the problem; #170 saw this
        surface as "Internal Server Error".
        """
        bot_id = TestBotLifecycle()._create(client)["id"]

        def _boom(*_args, **_kwargs):
            raise StreamingNotSupported(
                "coinbase does not support watchOHLCV, so it cannot stream candles."
            )

        monkeypatch.setattr("tradingbot.service.supervisor.build_venue", _boom)
        _login(client)
        response = client.post(f"/api/bots/{bot_id}/start", headers=_csrf(client))

        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "watchOHLCV" in detail
        assert "coinbase" in detail

    def test_unresolvable_contract_metadata_returns_a_readable_error(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#124 refuses the bot; the operator must be told why, not get a 500.

        This is the change most likely to stop a bot someone was relying on,
        so the message has to name the instrument and the missing fact.
        """
        from tradingbot.venues.contracts import ContractMetadataError

        bot_id = TestBotLifecycle()._create(client)["id"]

        def _boom(*_args, **_kwargs):
            raise ContractMetadataError(
                "MBTF6: exchange did not publish a contract size, so exposure "
                "cannot be computed; refusing rather than assuming 1.0"
            )

        monkeypatch.setattr("tradingbot.service.supervisor.build_venue", _boom)
        _login(client)
        response = client.post(f"/api/bots/{bot_id}/start", headers=_csrf(client))

        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "MBTF6" in detail
        assert "contract size" in detail


class TestTradePagination:
    def _bot_with_trades(self, client: TestClient, count: int) -> str:
        bot_id = TestBotLifecycle()._create(client)["id"]
        store = cast(FastAPI, client.app).state.store
        for n in range(count):
            store.append_trade(bot_id, {
                "bot_id": bot_id, "action": "buy", "status": "filled",
                "ok": True, "order_id": f"o{n}", "symbol": "BTC/USD", "ts": n,
            })
        return bot_id

    def test_pages_backward_through_history(self, client: TestClient) -> None:
        """Verify the cursor walks every trade exactly once, newest first."""
        bot_id = self._bot_with_trades(client, 12)
        seen: list[str] = []
        cursor: int | None = None
        for _ in range(10):
            url = f"/api/bots/{bot_id}/trades?limit=5"
            if cursor is not None:
                url += f"&before={cursor}"
            body = client.get(url, headers=_auth()).json()
            seen.extend(row["order_id"] for row in body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert seen == [f"o{n}" for n in range(11, -1, -1)]
        assert len(set(seen)) == 12

    def test_limit_above_the_cap_is_rejected(self, client: TestClient) -> None:
        """Verify the server enforces a maximum page size."""
        bot_id = self._bot_with_trades(client, 1)
        response = client.get(f"/api/bots/{bot_id}/trades?limit=100000", headers=_auth())
        assert response.status_code == 422

    def test_default_page_is_bounded(self, client: TestClient) -> None:
        """Verify a caller that passes no limit still gets a bounded page."""
        bot_id = self._bot_with_trades(client, 120)
        body = client.get(f"/api/bots/{bot_id}/trades", headers=_auth()).json()
        assert len(body["items"]) == 50
        assert body["next_cursor"] is not None


class TestAuthErrors:
    def test_non_bearer_scheme_returns_401(self, client: TestClient) -> None:
        """Verify that a non-Bearer authorization scheme is rejected."""
        response = client.get("/api/bots", headers={"Authorization": "Basic abc"})
        assert response.status_code == 401

    def test_invalid_token_returns_401(self, client: TestClient) -> None:
        """Verify that an invalid bearer token is rejected."""
        response = client.get("/api/bots", headers={"Authorization": "Bearer wrong-token"})
        assert response.status_code == 401

    def test_non_list_users_file_returns_401(self, client: TestClient) -> None:
        """Verify that a malformed users file falls back to no valid users."""
        store = cast(FastAPI, client.app).state.store
        store._users_file.write_text(json.dumps({"users": "not-a-list"}), encoding="utf-8")
        response = client.get("/api/bots", headers=_auth())
        assert response.status_code == 401


class TestPatchBot:
    def test_patch_bot_preserves_omitted_fields(self, client: TestClient) -> None:
        """Verify that omitted patch fields leave the bot unchanged."""
        bot = TestBotLifecycle()._create(client, live=False)
        bot_id = bot["id"]
        response = client.patch(f"/api/bots/{bot_id}", json={}, headers=_auth())
        assert response.status_code == 200
        assert response.json()["live"] is False

    def test_patch_unknown_bot_returns_404(self, client: TestClient) -> None:
        """Verify that patching an unknown bot returns 404."""
        response = client.patch("/api/bots/no-such-bot", json={"live": True}, headers=_auth())
        assert response.status_code == 404


class TestStopBot:
    def test_stop_unknown_bot_returns_404(self, client: TestClient) -> None:
        """Verify that stopping an unknown bot returns 404."""
        response = client.post("/api/bots/no-such-bot/stop", headers=_auth())
        assert response.status_code == 404


class TestWebSocketEvents:
    def test_ws_receives_decision_event(self, client: TestClient) -> None:
        """Verify that the WebSocket forwards decision events."""
        app = cast(FastAPI, client.app)
        supervisor = app.state.supervisor
        _login(client)
        with client.websocket_connect("/ws") as ws:
            _wait_for_subscribers(supervisor.event_bus)
            supervisor.event_bus.publish(
                DecisionEvent(bot_id="b1", symbol="BTC/USD", ts=1, text="no signal")
            )
            data = ws.receive_json()
            assert data["type"] == "decision"
            assert data["bot_id"] == "b1"

    def test_ws_receives_state_event(self, client: TestClient) -> None:
        """Verify the WebSocket forwards the authoritative bot-state snapshot."""
        app = cast(FastAPI, client.app)
        supervisor = app.state.supervisor
        _login(client)
        with client.websocket_connect("/ws") as ws:
            _wait_for_subscribers(supervisor.event_bus)
            supervisor.event_bus.publish(
                BotStateEvent(
                    bot_id="b1",
                    seq=7,
                    status="running",
                    position={"symbol": "BTC/USD", "side": "long", "size": 1.0, "entry_price": 10.0},
                    pnl=12.5,
                    last_decision="buy",
                    degraded=True,
                    degraded_reason="stream ended without an unsubscribe",
                )
            )
            data = ws.receive_json()
            assert data["type"] == "state"
            assert data["bot_id"] == "b1"
            assert data["seq"] == 7
            assert data["status"] == "running"
            assert data["pnl"] == 12.5
            assert data["position"]["side"] == "long"
            assert data["degraded"] is True
            assert data["degraded_reason"] == "stream ended without an unsubscribe"

    def test_ws_reports_overflow_so_the_client_resynchronizes(self, client: TestClient) -> None:
        """Verify a dropped-event notice reaches the browser."""
        app = cast(FastAPI, client.app)
        supervisor = app.state.supervisor
        _login(client)
        with client.websocket_connect("/ws") as ws:
            _wait_for_subscribers(supervisor.event_bus)
            supervisor.event_bus.publish(OverflowEvent(dropped=17))
            data = ws.receive_json()
            assert data["type"] == "overflow"
            assert data["dropped"] == 17

    def test_ws_receives_unknown_event(self, client: TestClient) -> None:
        """Verify that the WebSocket serializes unknown events safely."""
        app = cast(FastAPI, client.app)
        supervisor = app.state.supervisor
        _login(client)
        with client.websocket_connect("/ws") as ws:
            _wait_for_subscribers(supervisor.event_bus)
            supervisor.event_bus.publish("plain-string")
            data = ws.receive_json()
            assert data["type"] == "unknown"


def _supervisor_with_store(monkeypatch: pytest.MonkeyPatch, store: BotStore) -> BotSupervisor:
    """A supervisor wired to ``store`` so it can restore persisted bots."""
    monkeypatch.setattr("tradingbot.service.supervisor.build_venue", lambda *a, **k: _FakeVenue())
    monkeypatch.setattr("tradingbot.service.supervisor.build_strategy", lambda *a, **k: _SignalStrategy())
    return BotSupervisor(
        hub_factory=lambda cfg: _FakeHub(),
        event_bus=EventBus(),
        global_exposure=GlobalExposure(),
        store=store,
    )


_BOT_PAYLOAD = {
    "venue": "coinbase",
    "market_type": "spot",
    "strategy": "example",
    "symbol": "BTC/USD",
    "timeframe": "1m",
    "quantity": 0.1,
    "per_bot_cap": 1_000.0,
    "global_cap": 10_000.0,
    "params": {},
}


class TestRestartRestoresBots:
    def test_bot_created_before_restart_is_visible_after_restart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bot persisted by one process is served by the next one."""
        data_dir = tmp_path / "data"
        store = _store(tmp_path)
        first = create_app(store=store, supervisor=_supervisor_with_store(monkeypatch, store))
        with TestClient(first) as client:
            created = client.post("/api/bots", json=_BOT_PAYLOAD, headers=_auth())
            assert created.status_code == 201
            bot_id = created.json()["id"]

        # Restart: a brand-new store and supervisor over the same data directory.
        restarted_store = BotStore(data_dir)
        second = create_app(
            store=restarted_store,
            supervisor=_supervisor_with_store(monkeypatch, restarted_store),
        )
        with TestClient(second) as client:
            listed = client.get("/api/bots", headers=_auth())
            assert listed.status_code == 200
            bots = listed.json()
            assert [bot["id"] for bot in bots] == [bot_id]
            # Restored, but deliberately not trading.
            assert bots[0]["status"] == "stopped"

    def test_restored_bot_can_be_started(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A restored bot is fully operable, not just a listing entry."""
        data_dir = tmp_path / "data"
        store = _store(tmp_path)
        first = create_app(store=store, supervisor=_supervisor_with_store(monkeypatch, store))
        with TestClient(first) as client:
            bot_id = client.post("/api/bots", json=_BOT_PAYLOAD, headers=_auth()).json()["id"]

        restarted_store = BotStore(data_dir)
        second = create_app(
            store=restarted_store,
            supervisor=_supervisor_with_store(monkeypatch, restarted_store),
        )
        with TestClient(second) as client:
            started = client.post(f"/api/bots/{bot_id}/start", headers=_auth())
            assert started.status_code == 200
            assert started.json()["status"] == "running"
            client.post(f"/api/bots/{bot_id}/stop", headers=_auth())

    def test_malformed_record_does_not_hide_valid_bots(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One corrupt entry in bots.json must not blank the dashboard."""
        data_dir = tmp_path / "data"
        store = _store(tmp_path)
        first = create_app(store=store, supervisor=_supervisor_with_store(monkeypatch, store))
        with TestClient(first) as client:
            bot_id = client.post("/api/bots", json=_BOT_PAYLOAD, headers=_auth()).json()["id"]

        bots_file = data_dir / "bots.json"
        records = json.loads(bots_file.read_text(encoding="utf-8"))
        records.append({"id": "corrupt"})
        bots_file.write_text(json.dumps(records), encoding="utf-8")

        restarted_store = BotStore(data_dir)
        second = create_app(
            store=restarted_store,
            supervisor=_supervisor_with_store(monkeypatch, restarted_store),
        )
        with TestClient(second) as client:
            bots = client.get("/api/bots", headers=_auth()).json()
            assert [bot["id"] for bot in bots] == [bot_id]


class TestGracefulShutdown:
    def test_running_bot_is_stopped_when_the_app_shuts_down(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Leaving the app context stops running bots instead of abandoning them."""
        store = _store(tmp_path)
        supervisor = _supervisor_with_store(monkeypatch, store)
        app = create_app(store=store, supervisor=supervisor)
        with TestClient(app) as client:
            bot_id = client.post("/api/bots", json=_BOT_PAYLOAD, headers=_auth()).json()["id"]
            assert client.post(f"/api/bots/{bot_id}/start", headers=_auth()).status_code == 200
            bot = supervisor.get(bot_id)
            assert bot is not None and bot.status == "running"

        assert bot.status == "stopped"
        assert bot.task is None or bot.task.done()


class TestLifecycleConcurrency:
    """Concurrent lifecycle requests over the real ASGI app (issue #126)."""

    def _app_and_supervisor(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Build an app whose bots all share one hub, so duplicate runtimes show up."""
        store = _store(tmp_path)
        monkeypatch.setattr("tradingbot.service.supervisor.build_venue", lambda *a, **k: _FakeVenue())
        monkeypatch.setattr(
            "tradingbot.service.supervisor.build_strategy", lambda *a, **k: _SignalStrategy()
        )
        hub = _FakeHub()
        supervisor = BotSupervisor(
            hub_factory=lambda cfg: hub,
            event_bus=EventBus(),
            global_exposure=GlobalExposure(),
            store=store,
        )
        return create_app(store=store, supervisor=supervisor), supervisor, hub

    def test_concurrent_start_requests_start_one_runtime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two simultaneous POST /start calls must not build two runtimes."""
        app, supervisor, hub = self._app_and_supervisor(tmp_path, monkeypatch)
        with TestClient(app) as client:
            bot_id = client.post("/api/bots", json=_BOT_PAYLOAD, headers=_auth()).json()["id"]

        async def hammer() -> tuple[list[int], str, int, int]:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                results = await asyncio.gather(
                    ac.post(f"/api/bots/{bot_id}/start", headers=_auth()),
                    ac.post(f"/api/bots/{bot_id}/start", headers=_auth()),
                )
                bot = supervisor.get(bot_id)
                assert bot is not None
                # Observed inside the loop: closing it would cancel the task and
                # flip the status to stopped.
                observed = (
                    [r.status_code for r in results],
                    bot.status,
                    hub.warmups,
                    len(hub.handlers.get(("BTC/USD", "1m"), [])),
                )
                await supervisor.stop(bot_id)
                return observed

        codes, bot_status, warmups, subscriptions = asyncio.run(hammer())

        assert codes == [200, 200], "a repeated start must be idempotent, not an error"
        assert bot_status == "running"
        assert warmups == 1, "second start rebuilt the runtime"
        assert subscriptions == 1, "duplicate market subscription"

    def test_concurrent_stop_requests_are_idempotent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simultaneous stops both succeed and leave the bot stopped once."""
        app, supervisor, _hub = self._app_and_supervisor(tmp_path, monkeypatch)
        with TestClient(app) as client:
            bot_id = client.post("/api/bots", json=_BOT_PAYLOAD, headers=_auth()).json()["id"]
            client.post(f"/api/bots/{bot_id}/start", headers=_auth())

        async def hammer() -> list[int]:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
                results = await asyncio.gather(
                    ac.post(f"/api/bots/{bot_id}/stop", headers=_auth()),
                    ac.post(f"/api/bots/{bot_id}/stop", headers=_auth()),
                )
                return [r.status_code for r in results]

        assert asyncio.run(hammer()) == [200, 200]
        bot = supervisor.get(bot_id)
        assert bot is not None and bot.status == "stopped"
        assert bot.task is None

    def test_patch_during_a_transition_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A patch racing a start gets 409 instead of silently half-applying."""
        app, supervisor, _hub = self._app_and_supervisor(tmp_path, monkeypatch)
        with TestClient(app) as client:
            bot_id = client.post("/api/bots", json=_BOT_PAYLOAD, headers=_auth()).json()["id"]
        bot = supervisor.get(bot_id)
        assert bot is not None
        bot.status = "starting"

        with TestClient(app) as client:
            response = client.patch(
                f"/api/bots/{bot_id}", json={"per_bot_cap": 5.0}, headers=_auth()
            )

        assert response.status_code == 409
        assert bot.config.per_bot_cap == 1_000.0, "patch applied despite the 409"


class TestPatchWhileRunning:
    """A running bot's config is immutable (issue #109)."""

    def test_patch_while_running_is_rejected(self, client: TestClient) -> None:
        """Turning LIVE on for a running bot must not be silently accepted."""
        bot = client.post("/api/bots", json=_BOT_PAYLOAD, headers=_auth()).json()
        bot_id = bot["id"]
        client.post(f"/api/bots/{bot_id}/start", headers=_auth())

        response = client.patch(f"/api/bots/{bot_id}", json={"live": True}, headers=_auth())

        assert response.status_code == 409
        assert "running" in response.json()["detail"]

    def test_rejected_patch_leaves_the_advertised_config_unchanged(
        self, client: TestClient
    ) -> None:
        """The API never advertises a config the running bot is not executing."""
        bot_id = client.post("/api/bots", json=_BOT_PAYLOAD, headers=_auth()).json()["id"]
        client.post(f"/api/bots/{bot_id}/start", headers=_auth())

        client.patch(
            f"/api/bots/{bot_id}",
            json={"live": True, "per_bot_cap": 5.0, "global_cap": 7.0, "params": {"x": 1}},
            headers=_auth(),
        )

        view = client.get(f"/api/bots/{bot_id}", headers=_auth()).json()
        assert view["live"] is False
        assert view["per_bot_cap"] == 1_000.0
        assert view["global_cap"] == 10_000.0
        assert view["params"] == {}

    def test_turning_live_off_is_also_rejected_while_running(self, client: TestClient) -> None:
        """Even the risk-reducing direction is refused; stopping is the safe path.

        Accepting it would leave the already-built live venue able to send real
        orders while the UI showed dry-run — the worse of the two failures.
        """
        bot_id = client.post(
            "/api/bots", json={**_BOT_PAYLOAD, "live": True}, headers=_auth()
        ).json()["id"]
        client.patch(f"/api/bots/{bot_id}", json={"live": True}, headers=_auth())
        client.post(f"/api/bots/{bot_id}/start", headers=_auth())

        response = client.patch(f"/api/bots/{bot_id}", json={"live": False}, headers=_auth())

        assert response.status_code == 409

    def test_patch_applies_once_the_bot_is_stopped(self, client: TestClient) -> None:
        """Stop, edit, start is the supported flow and works end to end."""
        bot_id = client.post("/api/bots", json=_BOT_PAYLOAD, headers=_auth()).json()["id"]
        client.post(f"/api/bots/{bot_id}/start", headers=_auth())
        client.post(f"/api/bots/{bot_id}/stop", headers=_auth())

        response = client.patch(
            f"/api/bots/{bot_id}", json={"live": True, "per_bot_cap": 42.0}, headers=_auth()
        )

        assert response.status_code == 200
        assert response.json()["live"] is True
        assert response.json()["per_bot_cap"] == 42.0

    def test_patch_on_a_never_started_bot_still_works(self, client: TestClient) -> None:
        """The common case — configuring before the first start — is unaffected."""
        bot_id = client.post("/api/bots", json=_BOT_PAYLOAD, headers=_auth()).json()["id"]

        response = client.patch(f"/api/bots/{bot_id}", json={"per_bot_cap": 7.0}, headers=_auth())

        assert response.status_code == 200
        assert response.json()["per_bot_cap"] == 7.0
