import pytest
from tradingbot.config import load_config, Config, ConfigError, require_bybit_credentials


def test_defaults():
    cfg = load_config({})
    assert isinstance(cfg, Config)
    assert cfg.symbol == "BTCUSDT"
    assert cfg.timeframe == "5"
    assert cfg.order_qty == 0.001
    assert cfg.bybit_testnet is True
    assert cfg.bybit_api_key == ""


def test_reads_settings():
    cfg = load_config({
        "BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s", "BYBIT_TESTNET": "false",
        "SYMBOL": "ETHUSDT", "TIMEFRAME": "15", "ORDER_QTY": "0.01",
    })
    assert cfg.bybit_api_key == "k" and cfg.bybit_api_secret == "s"
    assert cfg.bybit_testnet is False
    assert cfg.symbol == "ETHUSDT" and cfg.timeframe == "15" and cfg.order_qty == 0.01


def test_require_credentials_raises_when_missing():
    with pytest.raises(ConfigError):
        require_bybit_credentials(load_config({}))


def test_require_credentials_ok_when_present():
    require_bybit_credentials(load_config({"BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s"}))
