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
