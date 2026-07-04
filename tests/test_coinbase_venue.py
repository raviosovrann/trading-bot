from tradingbot.models import Order, OrderType, Side, PositionSide
from tradingbot.venues.coinbase import CoinbaseVenue, _COINBASE_SANDBOX_BASE_URL


def test_place_order_success_mapping():
    class Client:
        def create_order(self, **kwargs):
            return {
                "success": True,
                "order_id": "cb-1",
                "status": "PENDING",
                "filled_size": "0.0",
            }

    venue = CoinbaseVenue(client=Client())
    result = venue.place_order(Order(symbol="BTC-USD", side=Side.buy, order_type=OrderType.market, qty=0.01))

    assert result.ok is True
    assert result.order_id == "cb-1"
    assert result.status == "pending"
    assert result.filled_qty == 0.0


def test_place_order_exception_returns_structured_failure():
    class Client:
        def create_order(self, **kwargs):
            raise RuntimeError("coinbase boom")

    venue = CoinbaseVenue(client=Client())
    result = venue.place_order(Order(symbol="BTC-USD", side=Side.buy, order_type=OrderType.market, qty=0.01))

    assert result.ok is False
    assert result.status == "error"
    assert "coinbase boom" in (result.error or "")


def test_get_position_long_mapping():
    class Client:
        def get_accounts(self):
            return {
                "accounts": [
                    {"currency": "USD", "available_balance": {"value": "1000", "currency": "USD"}},
                    {"currency": "BTC", "available_balance": {"value": "0.25", "currency": "BTC"}},
                ]
            }

    venue = CoinbaseVenue(client=Client())
    pos = venue.get_position("BTC-USD")

    assert pos is not None
    assert pos.side is PositionSide.long
    assert pos.size == 0.25
    assert pos.entry_price == 0.0


def test_close_position_noop_when_flat_or_none():
    class Client:
        def __init__(self):
            self.calls = 0

        def get_accounts(self):
            return {
                "accounts": [
                    {"currency": "BTC", "available_balance": {"value": "0", "currency": "BTC"}},
                ]
            }

        def create_order(self, **kwargs):
            self.calls += 1
            return {"success": True, "order_id": "should-not-happen", "status": "PENDING"}

    client = Client()
    venue = CoinbaseVenue(client=client)
    result = venue.close_position("BTC-USD")

    assert result.ok is True
    assert result.status == "no position"
    assert result.order_id is None
    assert client.calls == 0


def test_health_check_true_and_false_paths():
    class OkClient:
        def get_accounts(self):
            return {"accounts": []}

    class BadClient:
        def get_accounts(self):
            raise RuntimeError("down")

    assert CoinbaseVenue(client=OkClient()).health_check() is True
    assert CoinbaseVenue(client=BadClient()).health_check() is False


def test_from_credentials_switches_sandbox_and_production_hosts():
    # The coinbase-advanced-py RESTClient performs no network call at
    # construction and stores its host under the `base_url` attribute, so we
    # can assert host selection by inspecting the constructed client.
    sandbox_venue = CoinbaseVenue.from_credentials(api_key="k", api_secret="s", sandbox=True)
    assert sandbox_venue._client.base_url == _COINBASE_SANDBOX_BASE_URL
    assert sandbox_venue._client.base_url == "api-sandbox.coinbase.com"

    prod_venue = CoinbaseVenue.from_credentials(api_key="k", api_secret="s", sandbox=False)
    assert prod_venue._client.base_url == "api.coinbase.com"

