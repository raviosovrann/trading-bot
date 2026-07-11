import pytest
from tradingbot.config import (
    load_config,
    Config,
    ConfigError,
    require_credentials,
)


def test_defaults():
    cfg = load_config({})
    assert isinstance(cfg, Config)
    assert cfg.exchange == "coinbase"
    assert cfg.symbol == "BTC/USD"
    assert cfg.timeframe == "5m"
    assert cfg.order_qty == 0.001
    assert cfg.api_key == "" and cfg.api_secret == "" and cfg.api_password == ""
    assert cfg.stream is False
    assert cfg.live is False


def test_reads_exchange_settings():
    cfg = load_config({
        "EXCHANGE": "kraken",
        "API_KEY": "k", "API_SECRET": "s", "API_PASSWORD": "p",
        "SYMBOL": "DOGE/USD", "TIMEFRAME": "1m", "ORDER_QTY": "0.01",
    })
    assert cfg.exchange == "kraken"
    assert cfg.api_key == "k" and cfg.api_secret == "s" and cfg.api_password == "p"
    assert cfg.symbol == "DOGE/USD" and cfg.timeframe == "1m" and cfg.order_qty == 0.01


def test_exchange_lowercased():
    assert load_config({"EXCHANGE": "Coinbase"}).exchange == "coinbase"


def test_live_flag_defaults_false_and_parses_true():
    assert load_config({}).live is False
    assert load_config({"LIVE": "1"}).live is True
    assert load_config({"LIVE": "true"}).live is True


def test_stream_flag_defaults_false_and_parses_true():
    assert load_config({}).stream is False
    assert load_config({"STREAM": "yes"}).stream is True


def test_require_credentials_raises_when_missing():
    with pytest.raises(ConfigError):
        require_credentials(load_config({}))


def test_require_credentials_ok_when_present():
    require_credentials(load_config({"API_KEY": "k", "API_SECRET": "s"}))


def test_one_credential_missing_raises():
    with pytest.raises(ConfigError):
        require_credentials(load_config({"API_KEY": "k"}))


def test_repr_masks_secrets():
    r = repr(load_config({
        "API_KEY": "mykey123", "API_SECRET": "mysecret456", "API_PASSWORD": "mypass789",
    }))
    assert "mykey123" not in r and "mysecret456" not in r and "mypass789" not in r
    assert "***" in r


def test_unrecognized_bool_uses_safe_default():
    # A typo must never silently flip LIVE on.
    assert load_config({"LIVE": "ture"}).live is False
    assert load_config({"STREAM": "   "}).stream is False


def test_invalid_order_qty_raises_config_error():
    with pytest.raises(ConfigError):
        load_config({"ORDER_QTY": "abc"})


def test_empty_order_qty_uses_default():
    assert load_config({"ORDER_QTY": ""}).order_qty == 0.001
