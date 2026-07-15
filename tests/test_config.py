"""Tests for configuration loading and validation."""

import pytest
from tradingbot.config import (
    load_config,
    Config,
    ConfigError,
    require_credentials,
)


def test_defaults():
    """Verify default config values when no overrides are provided."""
    cfg = load_config({})
    assert isinstance(cfg, Config)
    assert cfg.exchange == "coinbase"
    assert cfg.symbol == "BTC/USD"
    assert cfg.timeframe == "5m"
    assert cfg.order_qty == 0.001
    assert cfg.strategy == "example"
    assert cfg.api_key == "" and cfg.api_secret == "" and cfg.api_password == ""
    assert cfg.stream is False
    assert cfg.live is False


def test_reads_exchange_settings():
    """Verify exchange and API settings are read from the config."""
    cfg = load_config({
        "EXCHANGE": "kraken",
        "API_KEY": "k", "API_SECRET": "s", "API_PASSWORD": "p",
        "SYMBOL": "DOGE/USD", "TIMEFRAME": "1m", "ORDER_QTY": "0.01",
    })
    assert cfg.exchange == "kraken"
    assert cfg.api_key == "k" and cfg.api_secret == "s" and cfg.api_password == "p"
    assert cfg.symbol == "DOGE/USD" and cfg.timeframe == "1m" and cfg.order_qty == 0.01


def test_reads_strategy_name():
    """Verify the strategy name is read and stripped from the config."""
    assert load_config({"STRATEGY": "  custom  "}).strategy == "custom"


def test_exchange_lowercased():
    """Verify the exchange name is normalized to lowercase."""
    assert load_config({"EXCHANGE": "Coinbase"}).exchange == "coinbase"


def test_live_flag_defaults_false_and_parses_true():
    """Verify the live flag defaults to false and parses common truthy strings."""
    assert load_config({}).live is False
    assert load_config({"LIVE": "1"}).live is True
    assert load_config({"LIVE": "true"}).live is True


def test_stream_flag_defaults_false_and_parses_true():
    """Verify the stream flag defaults to false and parses common truthy strings."""
    assert load_config({}).stream is False
    assert load_config({"STREAM": "yes"}).stream is True


def test_require_credentials_raises_when_missing():
    """Verify that require_credentials raises when credentials are missing."""
    with pytest.raises(ConfigError):
        require_credentials(load_config({}))


def test_require_credentials_ok_when_present():
    """Verify that require_credentials succeeds when credentials are present."""
    require_credentials(load_config({"API_KEY": "k", "API_SECRET": "s"}))


def test_one_credential_missing_raises():
    """Verify that a missing half of the credentials still raises an error."""
    with pytest.raises(ConfigError):
        require_credentials(load_config({"API_KEY": "k"}))


def test_repr_masks_secrets():
    """Verify that repr masks secret values."""
    r = repr(load_config({
        "API_KEY": "mykey123", "API_SECRET": "mysecret456", "API_PASSWORD": "mypass789",
    }))
    assert "mykey123" not in r and "mysecret456" not in r and "mypass789" not in r
    assert "***" in r


def test_unrecognized_bool_uses_safe_default():
    """Verify that invalid boolean strings safely default to false."""
    # A typo must never silently flip LIVE on.
    assert load_config({"LIVE": "ture"}).live is False
    assert load_config({"STREAM": "   "}).stream is False


def test_invalid_order_qty_raises_config_error():
    """Verify that a non-numeric order quantity raises a config error."""
    with pytest.raises(ConfigError):
        load_config({"ORDER_QTY": "abc"})


def test_empty_order_qty_uses_default():
    """Verify that an empty order quantity falls back to the default."""
    assert load_config({"ORDER_QTY": ""}).order_qty == 0.001
