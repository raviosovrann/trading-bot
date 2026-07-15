"""Venue and strategy builders used by the supervisor and API."""

from __future__ import annotations

from collections.abc import Callable

from ..strategies import (
    Strategy,
    StrategyContext,
    available_strategies as _available_strategies,
    build_strategy as _build_strategy,
)
from ..venues.base import ExecutionVenue
from ..venues.ccxt import CcxtVenue
from ..venues.tradovate import TradovateVenue

_Credentials = dict[str, object]
_VenueBuilder = Callable[[_Credentials, bool], ExecutionVenue]


def _build_coinbase(creds: _Credentials, live: bool) -> ExecutionVenue:
    """Build a Coinbase ccxt venue from stored credentials.

    Args:
        creds: Dictionary containing ``api_key`` and ``api_secret``.
        live: Whether to use live trading endpoints.

    Returns:
        Configured ``CcxtVenue``.

    Raises:
        ValueError: If required credentials are missing.
    """
    missing = [key for key in ("api_key", "api_secret") if not creds.get(key)]
    if missing:
        raise ValueError(f"Missing Coinbase credential(s): {', '.join(missing)}")
    return CcxtVenue.from_exchange(
        str(creds.get("exchange") or "coinbase"),
        str(creds["api_key"]),
        str(creds["api_secret"]),
        str(creds["api_password"]) if creds.get("api_password") else None,
        live=live,
    )


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


_VENUE_BUILDERS: dict[tuple[str, str], _VenueBuilder] = {
    ("coinbase", "spot"): _build_coinbase,
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


def available_venues() -> list[dict[str, str]]:
    """Return all supported venue/market-type mappings."""
    return [
        {"venue": venue, "market_type": market_type}
        for venue, market_type in sorted(_VENUE_BUILDERS)
    ]


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
