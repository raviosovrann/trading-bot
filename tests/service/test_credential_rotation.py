"""Credential rotation must actually reach the clients that use them (#137)."""

from __future__ import annotations

import pytest

from tradingbot.service.hub_factory import HubFactory
from tradingbot.service.supervisor import BotConfig


class _FakeStream:
    def __init__(self) -> None:
        self.stopped = 0

    def on_bar(self, handler): ...
    def on_bar_for(self, symbol, handler): ...
    async def run_async(self, *symbols): ...

    def stop(self) -> None:
        self.stopped += 1


class _FakeCandle:
    def warmup_candles(self, symbol, timeframe, limit):
        return []

    def latest_closed_candle(self, symbol, timeframe):
        return None


class _MutableStore:
    """Store whose secrets the test can rotate between calls."""

    def __init__(self, secrets: dict) -> None:
        self.secrets = secrets

    def load_secrets(self) -> dict:
        return self.secrets


def _cfg(bot_id: str, *, venue="coinbase", market_type="spot", timeframe="1h") -> BotConfig:
    return BotConfig(
        id=bot_id, venue=venue, market_type=market_type, strategy="example",
        symbol="BTC/USD", timeframe=timeframe, quantity=0.1, live=False,
        per_bot_cap=1000.0, global_cap=5000.0, params={},
    )


def _factory(store: _MutableStore, built: list) -> HubFactory:
    def feed_builder(venue, market_type, timeframe, creds):
        stream = _FakeStream()
        built.append({"venue": venue, "timeframe": timeframe, "creds": dict(creds), "stream": stream})
        return stream, _FakeCandle()

    return HubFactory(store, feed_builder=feed_builder)


def test_rotating_credentials_rebuilds_the_hub_with_the_new_set() -> None:
    """Verify the next hub is built from the rotated credentials.

    Without this the store holds the new key while the cached client keeps
    using the old one — rotation appears to succeed but does nothing.
    """
    store = _MutableStore({"coinbase": {"spot": {"api_key": "old", "api_secret": "s"}}})
    built: list = []
    factory = _factory(store, built)

    factory(_cfg("bot-1"))
    store.secrets = {"coinbase": {"spot": {"api_key": "new", "api_secret": "s"}}}
    factory(_cfg("bot-1"))

    assert [entry["creds"]["api_key"] for entry in built] == ["old", "new"]


def test_unchanged_credentials_still_share_one_hub() -> None:
    """Verify the cache is not defeated: identical credentials reuse the hub."""
    store = _MutableStore({"coinbase": {"spot": {"api_key": "k", "api_secret": "s"}}})
    built: list = []
    factory = _factory(store, built)

    first = factory(_cfg("bot-1"))
    second = factory(_cfg("bot-2"))

    assert first is second
    assert len(built) == 1


def test_rotation_closes_the_superseded_stream() -> None:
    """Verify the old client is stopped, not left reconnecting with a dead key."""
    store = _MutableStore({"coinbase": {"spot": {"api_key": "old", "api_secret": "s"}}})
    built: list = []
    factory = _factory(store, built)

    factory(_cfg("bot-1"))
    store.secrets = {"coinbase": {"spot": {"api_key": "new", "api_secret": "s"}}}
    factory(_cfg("bot-1"))

    assert built[0]["stream"].stopped == 1, "the superseded stream kept running"
    assert built[1]["stream"].stopped == 0


def test_rotation_leaves_other_venues_untouched() -> None:
    """Verify one venue's rotation does not disturb another's clients."""
    store = _MutableStore({
        "coinbase": {"spot": {"api_key": "old", "api_secret": "s"}},
        "kraken": {"spot": {"api_key": "kk", "api_secret": "ks"}},
    })
    built: list = []
    factory = _factory(store, built)

    coinbase_hub = factory(_cfg("bot-1"))
    kraken_hub = factory(_cfg("bot-2", venue="kraken"))
    store.secrets = {
        "coinbase": {"spot": {"api_key": "new", "api_secret": "s"}},
        "kraken": {"spot": {"api_key": "kk", "api_secret": "ks"}},
    }

    rotated = factory(_cfg("bot-1"))
    assert rotated is not coinbase_hub
    assert factory(_cfg("bot-2", venue="kraken")) is kraken_hub
    kraken_stream = next(e["stream"] for e in built if e["venue"] == "kraken")
    assert kraken_stream.stopped == 0


def test_rotation_invalidates_every_timeframe_for_that_account() -> None:
    """Verify all hubs on the rotated account are rebuilt, not just one.

    Hubs are cached per timeframe but credentials are per account, so a stale
    hub on another timeframe would keep the revoked key alive.
    """
    store = _MutableStore({"coinbase": {"spot": {"api_key": "old", "api_secret": "s"}}})
    built: list = []
    factory = _factory(store, built)

    factory(_cfg("bot-1", timeframe="1m"))
    factory(_cfg("bot-2", timeframe="1h"))
    store.secrets = {"coinbase": {"spot": {"api_key": "new", "api_secret": "s"}}}

    factory(_cfg("bot-1", timeframe="1m"))
    factory(_cfg("bot-2", timeframe="1h"))

    keys_by_timeframe: dict[str, list[str]] = {}
    for entry in built:
        keys_by_timeframe.setdefault(entry["timeframe"], []).append(entry["creds"]["api_key"])
    assert keys_by_timeframe["1m"] == ["old", "new"]
    assert keys_by_timeframe["1h"] == ["old", "new"]


def test_removing_credentials_also_invalidates() -> None:
    """Verify revoking credentials outright does not leave a live client."""
    store = _MutableStore({"coinbase": {"spot": {"api_key": "old", "api_secret": "s"}}})
    built: list = []
    factory = _factory(store, built)

    factory(_cfg("bot-1"))
    store.secrets = {}

    factory(_cfg("bot-1"))
    assert built[0]["stream"].stopped == 1
    assert len(built) == 2


def test_a_failed_rebuild_does_not_leave_a_stale_hub_cached() -> None:
    """Verify a broken replacement is not silently papered over by the old hub.

    Returning the superseded hub would mean a bot quietly keeps trading on
    credentials the operator believes they replaced.
    """
    store = _MutableStore({"coinbase": {"spot": {"api_key": "old", "api_secret": "s"}}})
    built: list = []
    factory = _factory(store, built)
    factory(_cfg("bot-1"))

    store.secrets = {"coinbase": {"spot": {"api_key": "bad", "api_secret": "s"}}}

    def _explode(venue, market_type, timeframe, creds):
        raise ValueError("invalid api key")

    factory._feed_builder = _explode  # type: ignore[attr-defined]
    with pytest.raises(ValueError):
        factory(_cfg("bot-1"))

    # The old hub must be gone, so a later good rotation builds cleanly.
    factory._feed_builder = lambda v, m, t, c: (  # type: ignore[attr-defined]
        built.append({"venue": v, "timeframe": t, "creds": dict(c), "stream": _FakeStream()})
        or (built[-1]["stream"], _FakeCandle())
    )
    store.secrets = {"coinbase": {"spot": {"api_key": "good", "api_secret": "s"}}}
    factory(_cfg("bot-1"))
    assert built[-1]["creds"]["api_key"] == "good"
