"""Resolve contract metadata from ccxt markets, with an explicit cache.

ccxt builds every market from a template in which *all* metadata fields start
as ``None``, and fills in only what the exchange actually publishes. So a
present key proves nothing and a missing ``contractSize`` is the ordinary case
rather than an exotic one. That is precisely why the old
``getattr(venue, "contract_multiplier", None)`` fallback to ``1.0`` was unsafe:
it could not distinguish "this contract is one unit" from "this exchange never
told us".

Everything here therefore raises ``ContractMetadataError`` rather than
returning a default. Refusing to start a bot is a recoverable failure; trading
one on a multiplier that is wrong by the contract size is not.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .contracts import ContractMetadataError, ContractSpec

_DEFAULT_TTL_SECONDS = 3600.0
"""How long resolved metadata stays valid.

Contract specifications are stable within a listing but not across one: dated
futures expire and are relisted with new sizes, and exchanges occasionally
re-denominate a perpetual. An hour keeps a rollover from being served stale
for a whole session without making metadata a per-order network cost.
"""


def _coerce_bool(value: Any) -> bool | None:
    """Return ``value`` as a bool, or ``None`` if the venue did not say.

    Distinguishing "false" from "unstated" matters here: an unstated
    linear/inverse flag is ambiguity, and ambiguity has to fail closed.
    """
    if isinstance(value, bool):
        return value
    return None


def spec_from_market(market: Any) -> ContractSpec:
    """Build a validated ``ContractSpec`` from one ccxt market dict.

    Args:
        market: A ccxt market structure.

    Returns:
        The instrument's validated spec.

    Raises:
        ContractMetadataError: If the market is not a mapping, omits the quote
            currency, or is a derivative whose contract size or linear/inverse
            convention the exchange did not publish.
    """
    if not isinstance(market, dict) or not market:
        raise ContractMetadataError("venue returned no market metadata")

    symbol = str(market.get("symbol") or "<unknown>")
    quote = str(market.get("quote") or "").strip()
    if not quote:
        raise ContractMetadataError(f"{symbol}: venue did not report a quote currency")

    is_derivative = bool(market.get("contract") or market.get("swap")
                         or market.get("future") or market.get("option"))
    precision = market.get("precision")
    tick = None
    if isinstance(precision, dict):
        raw_tick = precision.get("price")
        tick = float(raw_tick) if isinstance(raw_tick, (int, float)) else None

    if not is_derivative:
        # Spot: one unit is one unit of the base asset. The only case where a
        # multiplier of 1.0 is a fact rather than an assumption.
        return ContractSpec(
            symbol=symbol, contract_size=1.0, linear=True, quote_currency=quote,
            settle_currency=str(market.get("settle") or quote), tick_size=tick,
            is_derivative=False,
        )

    raw_size = market.get("contractSize")
    if not isinstance(raw_size, (int, float)):
        raise ContractMetadataError(
            f"{symbol}: exchange did not publish a contract size, so exposure "
            "cannot be computed; refusing rather than assuming 1.0"
        )

    linear = _coerce_bool(market.get("linear"))
    inverse = _coerce_bool(market.get("inverse"))
    if linear is None and inverse is None:
        raise ContractMetadataError(
            f"{symbol}: exchange did not say whether the contract is linear or "
            "inverse; the two differ by the price itself"
        )
    if linear and inverse:
        raise ContractMetadataError(
            f"{symbol}: exchange reports the contract as both linear and inverse"
        )
    is_linear = bool(linear) if linear is not None else not inverse

    return ContractSpec(
        symbol=symbol,
        contract_size=float(raw_size),
        linear=is_linear,
        quote_currency=quote,
        settle_currency=str(market.get("settle") or quote),
        tick_size=tick,
        is_derivative=True,
    )


class ContractCache:
    """Caches resolved contract metadata for one exchange client.

    Metadata is a network round trip, and exposure is checked on every order,
    so resolving per order would put an exchange call in the trading path. It
    is cached with a TTL rather than forever because contracts roll over --
    see ``_DEFAULT_TTL_SECONDS``.

    A failed load is never cached. Caching an error would turn one unreachable
    moment into a TTL-long outage for every bot on the venue.
    """

    def __init__(
        self,
        exchange: Any,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize the cache.

        Args:
            exchange: ccxt exchange client to load markets from.
            ttl_seconds: How long loaded metadata stays valid.
            clock: Monotonic time source; injectable for tests.
        """
        self._exchange = exchange
        self._ttl = ttl_seconds
        self._clock = clock
        self._markets: dict[str, Any] | None = None
        self._loaded_at = 0.0

    def spec(self, symbol: str) -> ContractSpec:
        """Return the validated spec for ``symbol``.

        Args:
            symbol: Instrument symbol as the venue names it.

        Returns:
            The instrument's contract spec.

        Raises:
            ContractMetadataError: If markets cannot be loaded, the symbol is
                not listed, or its metadata is incomplete.
        """
        markets = self._load()
        if symbol not in markets:
            raise ContractMetadataError(
                f"{symbol} is not listed on this venue, so its contract "
                "metadata cannot be resolved"
            )
        return spec_from_market(markets[symbol])

    def refresh(self) -> None:
        """Drop cached metadata so the next lookup reloads it.

        The explicit path for an expiry rollover or a relisting, without
        waiting out the TTL or restarting the service.
        """
        self._markets = None

    def _load(self) -> dict[str, Any]:
        """Return the market map, reloading when absent or expired.

        Raises:
            ContractMetadataError: If the exchange cannot be reached.
        """
        now = self._clock()
        if self._markets is not None and (now - self._loaded_at) < self._ttl:
            return self._markets
        try:
            markets = self._exchange.load_markets()
        except Exception as exc:
            raise ContractMetadataError(
                f"could not load contract metadata from the venue: {exc}"
            ) from exc
        if not isinstance(markets, dict):
            markets = getattr(self._exchange, "markets", None) or {}
        # Only recorded on success, so a failure does not start a TTL window
        # during which every bot on this venue is refused.
        self._markets = markets
        self._loaded_at = now
        return markets
