"""Hub factory: builds and shares MarketDataHubs across bots.

Rate limits are per account/IP, not per bot, so every bot on the same venue must
share one MarketDataHub (one set of streams, one rate limiter). This factory
caches a hub per ``(venue, market_type, timeframe)`` and a RateLimiter per
``(venue, market_type)`` account, so request volume scales with unique markets,
not bot count. Feeds are built lazily so tests can inject fakes (no network).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..datafeed import CcxtCandleFeed
from ..stream import CcxtStreamFeed
from ..tradovate_feed import TradovateStreamFeed
from .datahub import MarketDataHub
from .ratelimit import RateLimiter
from .supervisor import BotConfig

# (venue, market_type, timeframe, creds) -> (stream_feed, candle_feed)
FeedBuilder = Callable[[str, str, str, dict], tuple[Any, Any]]


def _default_feed_builder(venue: str, market_type: str, timeframe: str, creds: dict) -> tuple[Any, Any]:
    """Build streaming + candle feeds for a venue from stored credentials."""
    if venue == "tradovate":
        # Market data uses the mdAccessToken; the WebSocket client
        # (_TradovateMdClient) must be completed/verified on the Tradovate demo.
        # Accept both the normalized secrets key and Tradovate's raw auth field.
        md_token = str(creds.get("md_access_token") or creds.get("mdAccessToken") or "")
        if not md_token:
            raise ValueError(
                "Tradovate market data needs creds['md_access_token'] (or "
                "'mdAccessToken', from the Tradovate auth response). "
                "See tradingbot.tradovate_feed."
            )
        # Share one MD client between the stream and candle feeds: the stream's
        # warmup feed is a TradovateCandleFeed wrapping the same client, so we
        # reuse it as the hub's candle feed rather than opening a second socket.
        stream_feed = TradovateStreamFeed.from_credentials(md_token, timeframe=timeframe)
        candle_feed = stream_feed.warmup_feed
        return stream_feed, candle_feed

    exchange_id = str(creds.get("exchange") or venue)
    api_key = str(creds.get("api_key", ""))
    api_secret = str(creds.get("api_secret", ""))
    password = str(creds["api_password"]) if creds.get("api_password") else None
    stream_feed = CcxtStreamFeed.from_exchange(
        exchange_id, api_key, api_secret, password, timeframe=timeframe, market_type=market_type
    )
    candle_feed = CcxtCandleFeed.from_exchange(
        exchange_id, api_key, api_secret, password, market_type=market_type
    )
    return stream_feed, candle_feed


class HubFactory:
    """Callable ``(BotConfig) -> MarketDataHub`` that shares hubs across bots."""

    def __init__(
        self,
        store: Any,
        *,
        rate_per_sec: float = 8.0,
        burst: int = 8,
        feed_builder: FeedBuilder | None = None,
        mtf_cache_seconds: float = 60.0,
    ) -> None:
        self._store = store
        self._rate_per_sec = rate_per_sec
        self._burst = burst
        self._feed_builder = feed_builder or _default_feed_builder
        self._mtf_cache_seconds = mtf_cache_seconds
        self._hubs: dict[tuple[str, str, str], MarketDataHub] = {}
        self._limiters: dict[tuple[str, str], RateLimiter] = {}

    def __call__(self, cfg: BotConfig) -> MarketDataHub:
        venue = cfg.venue.strip().lower()
        market_type = cfg.market_type.strip().lower()
        timeframe = cfg.timeframe
        hub_key = (venue, market_type, timeframe)

        hub = self._hubs.get(hub_key)
        if hub is not None:
            return hub

        # One rate limiter per account (venue+market_type), shared across timeframes.
        limiter = self._limiters.setdefault((venue, market_type), RateLimiter(self._rate_per_sec, self._burst))
        creds = self._creds(venue, market_type)
        stream_feed, candle_feed = self._feed_builder(venue, market_type, timeframe, creds)
        hub = MarketDataHub(
            stream_feed=stream_feed,
            candle_feed=candle_feed,
            limiter=limiter,
            mtf_cache_seconds=self._mtf_cache_seconds,
        )
        self._hubs[hub_key] = hub
        return hub

    def _creds(self, venue: str, market_type: str) -> dict:
        secrets = self._store.load_secrets()
        venue_secrets = secrets.get(venue, {}) if isinstance(secrets, dict) else {}
        creds = venue_secrets.get(market_type, {}) if isinstance(venue_secrets, dict) else {}
        return creds if isinstance(creds, dict) else {}
