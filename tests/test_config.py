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
    assert cfg.venue == "alpaca"
    assert cfg.symbol == "BTC/USD"
    assert cfg.timeframe == "5Min"
    assert cfg.order_qty == 0.001
    assert cfg.alpaca_paper is True
    assert cfg.coinbase_sandbox is True
    assert cfg.alpaca_api_key == ""


def test_reads_alpaca_settings():
    cfg = load_config({
        "VENUE": "alpaca",
        "ALPACA_API_KEY": "k", "ALPACA_API_SECRET": "s", "ALPACA_PAPER": "false",
        "SYMBOL": "ETH/USD", "TIMEFRAME": "15Min", "ORDER_QTY": "0.01",
    })
    assert cfg.venue == "alpaca"
    assert cfg.alpaca_api_key == "k" and cfg.alpaca_api_secret == "s"
    assert cfg.alpaca_paper is False
    assert cfg.symbol == "ETH/USD" and cfg.timeframe == "15Min" and cfg.order_qty == 0.01


def test_reads_coinbase_settings():
    cfg = load_config({
        "VENUE": "coinbase",
        "COINBASE_API_KEY": "k", "COINBASE_API_SECRET": "s", "COINBASE_SANDBOX": "false",
    })
    assert cfg.venue == "coinbase"
    assert cfg.coinbase_api_key == "k" and cfg.coinbase_api_secret == "s"
    assert cfg.coinbase_sandbox is False


def test_invalid_venue_raises():
    with pytest.raises(ConfigError):
        load_config({"VENUE": "bybit"})


def test_require_alpaca_credentials_raises_when_missing():
    with pytest.raises(ConfigError):
        require_credentials(load_config({"VENUE": "alpaca"}))


def test_require_alpaca_credentials_ok_when_present():
    require_credentials(load_config({
        "VENUE": "alpaca", "ALPACA_API_KEY": "k", "ALPACA_API_SECRET": "s",
    }))


def test_require_coinbase_credentials_raises_when_missing():
    with pytest.raises(ConfigError):
        require_credentials(load_config({"VENUE": "coinbase"}))


def test_require_coinbase_credentials_ok_when_present():
    require_credentials(load_config({
        "VENUE": "coinbase", "COINBASE_API_KEY": "k", "COINBASE_API_SECRET": "s",
    }))


def test_fake_venue_needs_no_credentials():
    require_credentials(load_config({"VENUE": "fake"}))


def test_repr_masks_secrets():
    r = repr(load_config({
        "ALPACA_API_KEY": "mykey123", "ALPACA_API_SECRET": "mysecret456",
        "COINBASE_API_KEY": "cbkey789", "COINBASE_API_SECRET": "cbsecret012",
    }))
    assert "mykey123" not in r and "mysecret456" not in r
    assert "cbkey789" not in r and "cbsecret012" not in r
    assert "***" in r


def test_unrecognized_bool_uses_safe_default():
    assert load_config({"ALPACA_PAPER": "fasle"}).alpaca_paper is True
    assert load_config({"COINBASE_SANDBOX": "   "}).coinbase_sandbox is True


def test_explicit_false_bools():
    cfg = load_config({"ALPACA_PAPER": "false", "COINBASE_SANDBOX": "no"})
    assert cfg.alpaca_paper is False
    assert cfg.coinbase_sandbox is False


def test_invalid_order_qty_raises_config_error():
    with pytest.raises(ConfigError):
        load_config({"ORDER_QTY": "abc"})


def test_empty_order_qty_uses_default():
    assert load_config({"ORDER_QTY": ""}).order_qty == 0.001


def test_one_credential_missing_raises():
    with pytest.raises(ConfigError):
        require_credentials(load_config({"VENUE": "alpaca", "ALPACA_API_KEY": "k"}))
