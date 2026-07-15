"""Tests for venue and strategy registry builders."""

from __future__ import annotations

import pytest

import tradingbot.service.registry as registry
from tradingbot.service.registry import (
    available_strategies,
    available_venues,
    build_strategy,
    build_venue,
)
from tradingbot.strategies import StrategyContext
from tradingbot.venues.ccxt import CcxtVenue
from tradingbot.venues.tradovate import TradovateVenue


def _context() -> StrategyContext:
    return StrategyContext(
        symbol="BTC/USD",
        timeframe="1m",
        quantity=0.1,
        data_feed=None,
        params={},
    )


def test_coinbase_spot_builds_ccxt_venue(monkeypatch) -> None:
    """Verify that coinbase spot builds a CcxtVenue with the provided credentials."""
    sentinel = object()
    calls = {}

    def fake_from_exchange(cls, *args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr(CcxtVenue, "from_exchange", classmethod(fake_from_exchange))

    result = build_venue(
        "coinbase",
        "spot",
        creds={
            "exchange": "coinbase",
            "api_key": "key",
            "api_secret": "secret",
            "api_password": "pass",
        },
        live=True,
    )

    assert result is sentinel
    assert calls == {
        "args": ("coinbase", "key", "secret", "pass"),
        "kwargs": {"live": True, "market_type": "spot"},
    }


def test_coinbase_missing_credentials_raise_helpful_value_error() -> None:
    """Verify that missing coinbase credentials raise a helpful ValueError."""
    with pytest.raises(ValueError, match="api_key.*api_secret"):
        build_venue("coinbase", "spot", creds={}, live=False)


def test_tradovate_futures_builds_tradovate_venue(monkeypatch) -> None:
    """Verify that tradovate futures builds a TradovateVenue with the provided credentials."""
    sentinel = object()
    calls = {}

    def fake_from_credentials(cls, **kwargs):
        calls.update(kwargs)
        return sentinel

    monkeypatch.setattr(
        TradovateVenue,
        "from_credentials",
        classmethod(fake_from_credentials),
    )
    creds = {
        "name": "trader",
        "password": "secret",
        "app_id": "app",
        "app_version": "1.0",
        "cid": "cid",
        "sec": "sec",
    }

    result = build_venue("tradovate", "futures", creds=creds, live=False)

    assert result is sentinel
    assert calls == {**creds, "live": False}


def test_tradovate_live_credential_is_overridden_by_argument(monkeypatch) -> None:
    """Verify that the live flag overrides the credential-provided live value."""
    calls = {}

    def fake_from_credentials(cls, **kwargs):
        calls.update(kwargs)
        return object()

    monkeypatch.setattr(
        TradovateVenue,
        "from_credentials",
        classmethod(fake_from_credentials),
    )

    build_venue(
        "tradovate",
        "futures",
        creds={"name": "trader", "live": True},
        live=False,
    )

    assert calls["live"] is False


@pytest.mark.parametrize(
    ("venue", "market_type"),
    [("coinbase", "options"), ("tradovate", "spot"), ("unknown", "spot")],
)
def test_unknown_venue_mapping_raises(venue: str, market_type: str) -> None:
    """Verify that unsupported venue/market type mappings raise a ValueError."""
    with pytest.raises(ValueError, match="Unsupported venue"):
        build_venue(venue, market_type, creds={}, live=False)


def test_available_venues_lists_supported_mappings() -> None:
    """Verify that available_venues lists the supported venue mappings."""
    assert available_venues() == [
        {"venue": "coinbase", "market_type": "futures"},
        {"venue": "coinbase", "market_type": "spot"},
        {"venue": "tradovate", "market_type": "futures"},
    ]


def test_available_venues_is_sorted_independently_of_mapping_order(monkeypatch) -> None:
    """Verify that available_venues is sorted regardless of mapping order."""
    monkeypatch.setattr(
        registry,
        "_VENUE_BUILDERS",
        {
            ("tradovate", "futures"): object(),
            ("coinbase", "spot"): object(),
        },
    )

    assert available_venues() == [
        {"venue": "coinbase", "market_type": "spot"},
        {"venue": "tradovate", "market_type": "futures"},
    ]


def test_strategy_registry_passes_through_plugin_registry() -> None:
    """Verify that the strategy registry passes through to the plugin registry."""
    assert "example" in available_strategies()
    assert build_strategy("example", _context()).on_bar([]) is None


def test_coinbase_futures_builds_ccxt_venue_with_futures_market_type(monkeypatch) -> None:
    """Verify coinbase futures maps to a ccxt venue built with market_type=futures."""
    sentinel = object()
    calls: dict = {}

    def fake_from_exchange(cls, *args, **kwargs):
        calls["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr(CcxtVenue, "from_exchange", classmethod(fake_from_exchange))
    result = build_venue(
        "coinbase", "futures",
        creds={"api_key": "k", "api_secret": "s"}, live=True,
    )
    assert result is sentinel
    assert calls["kwargs"]["market_type"] == "futures"
    assert calls["kwargs"]["live"] is True


def test_available_venues_includes_coinbase_futures() -> None:
    """Verify coinbase futures is advertised as a supported mapping."""
    pairs = {(v["venue"], v["market_type"]) for v in available_venues()}
    assert ("coinbase", "spot") in pairs
    assert ("coinbase", "futures") in pairs
    assert ("tradovate", "futures") in pairs
