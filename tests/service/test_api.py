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
from tradingbot.service.events import DecisionEvent, EventBus, OrderEvent
from tradingbot.service.risk import GlobalExposure
from tradingbot.service.store import BotStore
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
        assert response.json() == [{
            "bot_id": bot_id, "action": "buy", "status": "submitted",
            "ok": True, "order_id": "o1", "symbol": "BTC/USD", "ts": 42,
        }]

    def test_get_trades_tolerates_partial_records(self, client: TestClient) -> None:
        """A legacy/partial trade record is coerced, not 500'd."""
        bot = TestBotLifecycle()._create(client)
        bot_id = bot["id"]
        store = cast(FastAPI, client.app).state.store
        store.append_trade(bot_id, {"action": "sell", "status": "filled"})
        response = client.get(f"/api/bots/{bot_id}/trades", headers=_auth())
        assert response.status_code == 200
        row = response.json()[0]
        assert row["action"] == "sell" and row["ok"] is False and row["order_id"] is None


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
