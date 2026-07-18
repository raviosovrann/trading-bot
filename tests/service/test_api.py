"""Tests for the REST API and WebSocket endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import cast

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
    def test_valid_credentials_return_a_working_token(self, client: TestClient) -> None:
        """Login with correct credentials returns a token that authenticates."""
        response = client.post("/api/login", json={"username": _USERNAME, "password": _PASSWORD})
        assert response.status_code == 200
        token = response.json()["token"]
        assert token
        # The freshly minted token authenticates a protected request.
        authed = client.get("/api/bots", headers={"Authorization": f"Bearer {token}"})
        assert authed.status_code == 200

    def test_wrong_password_is_rejected(self, client: TestClient) -> None:
        """Login with a bad password returns 401 and no token."""
        response = client.post("/api/login", json={"username": _USERNAME, "password": "wrong"})
        assert response.status_code == 401
        assert "token" not in response.json()

    def test_unknown_user_is_rejected(self, client: TestClient) -> None:
        """Login with an unknown username returns 401."""
        response = client.post("/api/login", json={"username": "nobody", "password": _PASSWORD})
        assert response.status_code == 401

    def test_login_uses_atomic_user_update(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Token rotation does not replace a separately loaded users snapshot."""
        store = client.app.state.store  # type: ignore[attr-defined]

        def reject_snapshot_replace(data: dict) -> None:
            del data
            raise AssertionError("login must update its user inside the store transaction")

        monkeypatch.setattr(store, "save_users", reject_snapshot_replace)
        response = client.post(
            "/api/login",
            json={"username": _USERNAME, "password": _PASSWORD},
        )
        assert response.status_code == 200

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
        store = app.state.store
        supervisor = app.state.supervisor
        # Authenticate the WS with the same bearer token via query param.
        with client.websocket_connect(f"/ws?token={_TOKEN}") as ws:
            _wait_for_subscribers(supervisor.event_bus)
            supervisor.event_bus.publish(
                OrderEvent(bot_id="b1", action="buy", status="filled", ok=True, order_id="1")
            )
            data = ws.receive_json()
            assert data["type"] == "order"
            assert data["bot_id"] == "b1"
            assert data["action"] == "buy"

    def test_ws_without_token_is_closed(self, client: TestClient) -> None:
        """Verify that WebSocket connections without a token are closed."""
        with client.websocket_connect("/ws") as ws:
            # FastAPI closes with code 1008 when authentication fails.
            with pytest.raises(Exception):
                ws.receive_json()


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
        with client.websocket_connect(f"/ws?token={_TOKEN}") as ws:
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
        with client.websocket_connect(f"/ws?token={_TOKEN}") as ws:
            _wait_for_subscribers(supervisor.event_bus)
            supervisor.event_bus.publish("plain-string")
            data = ws.receive_json()
            assert data["type"] == "unknown"
