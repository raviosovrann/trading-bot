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


def test_repr_masks_secret():
    r = repr(load_config({"BYBIT_API_KEY": "mykey123", "BYBIT_API_SECRET": "mysecret456"}))
    assert "mysecret456" not in r and "mykey123" not in r
    assert "***" in r


def test_unrecognized_testnet_uses_safe_default():
    assert load_config({"BYBIT_TESTNET": "fasle"}).bybit_testnet is True
    assert load_config({"BYBIT_TESTNET": "   "}).bybit_testnet is True


def test_explicit_false_testnet():
    assert load_config({"BYBIT_TESTNET": "false"}).bybit_testnet is False


def test_invalid_order_qty_raises_config_error():
    with pytest.raises(ConfigError):
        load_config({"ORDER_QTY": "abc"})


def test_empty_order_qty_uses_default():
    assert load_config({"ORDER_QTY": ""}).order_qty == 0.001


def test_one_credential_missing_raises():
    with pytest.raises(ConfigError):
        require_bybit_credentials(load_config({"BYBIT_API_KEY": "k"}))
