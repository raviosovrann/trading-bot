"""Venue and strategy builders used by the supervisor and API."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..strategies import (
    Strategy,
    StrategyContext,
    available_strategies as _available_strategies,
    build_strategy as _build_strategy,
)
from ..models import OrderType
from ..venues.base import ExecutionVenue
from ..venues.capabilities import VenueCapabilities
from ..venues.ccxt import CcxtVenue
from ..venues.tradovate import TradovateVenue

_Credentials = dict[str, object]
_VenueBuilder = Callable[[_Credentials, bool], ExecutionVenue]


def _ccxt_builder(market_type: str) -> _VenueBuilder:
    """Return a builder for a ccxt venue of the given market type (spot|futures).

    The exchange id defaults to ``coinbase`` but is overridable via
    ``creds["exchange"]`` — Coinbase perpetual futures live on a different ccxt
    id (e.g. ``coinbaseinternational``) with its own credentials.
    """

    def build(creds: _Credentials, live: bool) -> ExecutionVenue:
        """Construct the venue, refusing incomplete credentials.

        Args:
            creds: Venue credentials. ``api_key`` and ``api_secret`` are
                required; ``api_password`` and ``exchange`` are optional.
            live: Arms real orders.

        Returns:
            The configured venue.

        Raises:
            ValueError: If a required credential is missing. Checked here so
                the failure names the field, rather than surfacing later as an
                opaque authentication error from the exchange.
        """
        missing = [key for key in ("api_key", "api_secret") if not creds.get(key)]
        if missing:
            raise ValueError(f"Missing ccxt credential(s): {', '.join(missing)}")
        return CcxtVenue.from_exchange(
            str(creds.get("exchange") or "coinbase"),
            str(creds["api_key"]),
            str(creds["api_secret"]),
            str(creds["api_password"]) if creds.get("api_password") else None,
            live=live,
            market_type=market_type,
            # The venue needs its own limits to refuse a reduce-only order it
            # cannot have enforced (#121). The exchange id is overridable, so
            # capabilities are looked up by the declared venue name.
            capabilities=venue_capabilities("coinbase", market_type),
        )

    return build


def _build_tradovate(creds: _Credentials, live: bool) -> ExecutionVenue:
    """Build a Tradovate venue from stored credentials.

    Args:
        creds: Dictionary with Tradovate authentication fields.
        live: Whether to use live trading endpoints.

    Returns:
        Configured ``TradovateVenue``.

    Raises:
        ValueError: If credentials are invalid or incomplete.
    """
    request = {key: value for key, value in creds.items() if key != "live"}
    try:
        return TradovateVenue.from_credentials(**request, live=live)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValueError(f"Invalid Tradovate credentials: {exc}") from exc


_SPOT_CAPABILITIES = dict(
    supports_short=False,
    # Spot has no position to reduce: a sell disposes of inventory, and no
    # exchange-enforced reduce-only flag exists for it.
    supports_reduce_only=False,
    order_types=frozenset({OrderType.market, OrderType.limit}),
)
_DERIVATIVE_CAPABILITIES = dict(
    supports_short=True,
    supports_reduce_only=True,
    order_types=frozenset({OrderType.market, OrderType.limit}),
)

_VENUE_CAPABILITIES: dict[tuple[str, str], dict] = {
    ("coinbase", "spot"): _SPOT_CAPABILITIES,
    ("coinbase", "futures"): _DERIVATIVE_CAPABILITIES,
    ("tradovate", "futures"): _DERIVATIVE_CAPABILITIES,
}
"""What each supported pair can do (#125).

Kept beside the builders so adding a venue without declaring its
capabilities is immediately visible -- a test asserts every supported
mapping has an entry.
"""

_VENUE_BUILDERS: dict[tuple[str, str], _VenueBuilder] = {
    ("coinbase", "spot"): _ccxt_builder("spot"),
    ("coinbase", "futures"): _ccxt_builder("futures"),
    ("tradovate", "futures"): _build_tradovate,
}


def build_venue(
    venue: str,
    market_type: str,
    *,
    creds: dict,
    live: bool,
) -> ExecutionVenue:
    """Build an execution venue for the given ``venue``/``market_type`` pair.

    Args:
        venue: Venue identifier, e.g. ``coinbase``.
        market_type: Market type, e.g. ``spot``.
        creds: Stored credentials required by the venue.
        live: Whether the venue should use live trading endpoints.

    Returns:
        An ``ExecutionVenue`` instance.

    Raises:
        ValueError: If the venue/market-type mapping is not supported.
    """
    key = (venue.strip().lower(), market_type.strip().lower())
    builder = _VENUE_BUILDERS.get(key)
    if builder is None:
        raise ValueError(f"Unsupported venue mapping: {venue!r}/{market_type!r}")
    return builder(creds, live)


def venue_capabilities(venue: str, market_type: str) -> VenueCapabilities:
    """Return what the given venue/market pair can do.

    Args:
        venue: Venue identifier.
        market_type: Market type.

    Returns:
        The pair's declared capabilities.

    Raises:
        ValueError: If the mapping is not supported.
    """
    key = (venue.strip().lower(), market_type.strip().lower())
    traits = _VENUE_CAPABILITIES.get(key)
    if traits is None:
        raise ValueError(f"Unsupported venue mapping: {venue!r}/{market_type!r}")
    return VenueCapabilities(venue=key[0], market_type=key[1], **traits)


def available_venues() -> list[dict[str, Any]]:
    """Return all supported venue/market-type mappings and their capabilities.

    Capabilities are included so the UI can filter incompatible choices rather
    than offering a pairing the API will refuse (#125).
    """
    listing: list[dict[str, Any]] = []
    for venue, market_type in sorted(_VENUE_BUILDERS):
        caps = venue_capabilities(venue, market_type)
        listing.append({
            "venue": venue,
            "market_type": market_type,
            "supports_short": caps.supports_short,
            "supports_reduce_only": caps.supports_reduce_only,
            "order_types": sorted(t.value for t in caps.order_types),
        })
    return listing


def build_strategy(name: str, ctx: StrategyContext) -> Strategy:
    """Build a strategy by delegating to the strategy registry.

    Args:
        name: Registered strategy name.
        ctx: Runtime context for the strategy.

    Returns:
        A strategy instance implementing the ``Strategy`` protocol.
    """
    return _build_strategy(name, ctx)


def available_strategies() -> list[str]:
    """Return all registered strategy names in alphabetical order."""
    return _available_strategies()
