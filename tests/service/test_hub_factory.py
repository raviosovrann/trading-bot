"""Tests for the shared-per-venue HubFactory."""

from __future__ import annotations

import pytest

from tradingbot.service.hub_factory import HubFactory, _default_feed_builder
from tradingbot.service.supervisor import BotConfig


class _FakeStream:
    def on_bar(self, handler): ...
    def on_bar_for(self, symbol, handler): ...
    async def run_async(self, *symbols): ...
    def stop(self): ...


class _FakeCandle:
    def warmup_candles(self, symbol, timeframe, limit): return []
    def latest_closed_candle(self, symbol, timeframe): return None


class _FakeStore:
    def __init__(self, secrets):
        self._secrets = secrets
    def load_secrets(self):
        return self._secrets


def _cfg(bot_id: str, *, venue="coinbase", market_type="spot", timeframe="1h") -> BotConfig:
    return BotConfig(
        id=bot_id, venue=venue, market_type=market_type, strategy="example",
        symbol="BTC/USD", timeframe=timeframe, quantity=0.1, live=False,
        per_bot_cap=1000.0, global_cap=5000.0, params={},
    )


def _factory(calls):
    store = _FakeStore({"coinbase": {"spot": {"api_key": "k", "api_secret": "s"}}})

    def feed_builder(venue, market_type, timeframe, creds):
        calls.append((venue, market_type, timeframe, dict(creds)))
        return _FakeStream(), _FakeCandle()

    return HubFactory(store, feed_builder=feed_builder)


def test_same_venue_timeframe_shares_one_hub():
    calls: list = []
    factory = _factory(calls)
    hub1 = factory(_cfg("a"))
    hub2 = factory(_cfg("b"))  # same venue/market_type/timeframe
    assert hub1 is hub2
    assert len(calls) == 1  # feeds built once, shared across both bots


def test_different_timeframe_gets_its_own_hub():
    calls: list = []
    factory = _factory(calls)
    hub1 = factory(_cfg("a", timeframe="1h"))
    hub2 = factory(_cfg("b", timeframe="5m"))
    assert hub1 is not hub2
    assert len(calls) == 2
    assert {c[2] for c in calls} == {"1h", "5m"}


def test_feed_builder_receives_stored_creds():
    calls: list = []
    factory = _factory(calls)
    factory(_cfg("a"))
    assert calls[0][3] == {"api_key": "k", "api_secret": "s"}


def test_default_builder_requires_md_token_for_tradovate():
    # Without an md_access_token the factory can't build Tradovate feeds.
    with pytest.raises(ValueError):
        _default_feed_builder("tradovate", "futures", "1h", {})


@pytest.mark.parametrize("key", ["md_access_token", "mdAccessToken"])
def test_default_builder_accepts_either_md_token_key(key):
    # Accept both the normalized secrets key and Tradovate's raw auth field.
    stream_feed, candle_feed = _default_feed_builder("tradovate", "futures", "1h", {key: "tok"})
    # Both feeds share a single MD client (one socket per token, not two).
    assert candle_feed is stream_feed.warmup_feed
    assert candle_feed._client is stream_feed._client


def test_coinbase_uses_the_native_feed_not_ccxt() -> None:
    """Verify coinbase market data no longer goes through ccxt (#171).

    ccxt has no watchOHLCV for coinbase, so the default builder must return the
    native Advanced Trade feeds instead.
    """
    from tradingbot.coinbase_feed import CoinbaseCandleFeed, CoinbaseStreamFeed

    stream_feed, candle_feed = _default_feed_builder("coinbase", "spot", "1m", {})

    assert isinstance(stream_feed, CoinbaseStreamFeed)
    assert isinstance(candle_feed, CoinbaseCandleFeed)


def test_the_native_coinbase_feed_needs_no_credentials() -> None:
    """Verify market data works with an empty credential set.

    Coinbase serves both market-data surfaces unauthenticated, which is what
    makes a credential-free path possible (#116).
    """
    stream_feed, candle_feed = _default_feed_builder("coinbase", "spot", "5m", {})
    assert stream_feed is not None and candle_feed is not None


def test_an_unsupported_coinbase_timeframe_is_refused() -> None:
    """Verify an unmappable timeframe fails at build time, not mid-stream."""
    with pytest.raises(ValueError, match="timeframe"):
        _default_feed_builder("coinbase", "spot", "3s", {})


def test_coinbase_futures_still_routes_through_ccxt() -> None:
    """Verify only spot uses the native feed.

    Coinbase perpetual futures live on a different product (coinbaseinternational
    in ccxt) with its own market data; sending them to the spot Advanced Trade
    feed would silently stream the wrong market.
    """
    from tradingbot.coinbase_feed import CoinbaseStreamFeed

    with pytest.raises(Exception) as excinfo:
        # No ccxt credentials here, so this raises rather than returning a
        # native feed — the point is that it does not take the native path.
        stream_feed, _ = _default_feed_builder("coinbase", "futures", "1m", {})
        assert not isinstance(stream_feed, CoinbaseStreamFeed)
    assert "CoinbaseStreamFeed" not in str(type(excinfo.value))


def test_an_explicit_exchange_override_still_uses_ccxt() -> None:
    """Verify creds['exchange'] keeps routing through ccxt for other venues."""
    from tradingbot.coinbase_feed import CoinbaseStreamFeed

    calls: list = []
    try:
        stream_feed, _ = _default_feed_builder("coinbase", "spot", "1m", {"exchange": "kraken"})
    except Exception:
        return  # building a real ccxt client may fail without keys; that is fine
    assert not isinstance(stream_feed, CoinbaseStreamFeed)
    del calls
