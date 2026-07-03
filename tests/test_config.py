import pytest
from tradingbot.config import load_config, Config, ConfigError


def test_load_config_reads_values():
    cfg = load_config({"WEBHOOK_TOKEN": "secret", "VENUE": "bybit_testnet", "ALLOWED_IPS": "1.2.3.4, 5.6.7.8"})
    assert isinstance(cfg, Config)
    assert cfg.webhook_token == "secret"
    assert cfg.venue == "bybit_testnet"
    assert cfg.allowed_ips == ("1.2.3.4", "5.6.7.8")


def test_venue_defaults_when_absent():
    cfg = load_config({"WEBHOOK_TOKEN": "secret"})
    assert cfg.venue == "bybit_testnet"
    assert cfg.allowed_ips == ()


def test_missing_token_raises():
    with pytest.raises(ConfigError):
        load_config({"VENUE": "bybit_testnet"})
