"""Resolving contract metadata from a venue, and caching it (#124).

ccxt initialises every market field to ``None`` and only fills in what the
exchange actually publishes. So "the key is present" proves nothing, and the
absent case is not exotic -- it is the default. These tests pin that an
underspecified derivative raises rather than resolving to something plausible.
"""

from __future__ import annotations

import pytest

from tradingbot.venues.contracts import ContractMetadataError, ContractSpec
from tradingbot.venues.ccxt_contracts import ContractCache, spec_from_market


def _market(**overrides) -> dict:
    """A ccxt market dict for a linear perpetual, before overrides."""
    market = {
        "symbol": "BTC/USDT:USDT",
        "base": "BTC",
        "quote": "USDT",
        "settle": "USDT",
        "contract": True,
        "spot": False,
        "swap": True,
        "linear": True,
        "inverse": False,
        "contractSize": 0.001,
        "precision": {"price": 0.1, "amount": 0.001},
    }
    market.update(overrides)
    return market


class TestResolution:
    def test_a_linear_perpetual_resolves(self) -> None:
        spec = spec_from_market(_market())

        assert spec.contract_size == 0.001
        assert spec.linear is True
        assert spec.quote_currency == "USDT"
        assert spec.tick_size == 0.1
        assert spec.is_derivative is True

    def test_an_inverse_perpetual_resolves(self) -> None:
        spec = spec_from_market(
            _market(linear=False, inverse=True, settle="BTC", contractSize=100.0)
        )

        assert spec.linear is False
        assert spec.settle_currency == "BTC"
        # Price-independent, which is the point of the linear flag.
        assert spec.notional(2.0, 50_000.0) == pytest.approx(200.0)

    def test_a_spot_market_resolves_without_a_contract_size(self) -> None:
        # Spot has no contractSize in ccxt, and needs none: one unit is one
        # unit of the base asset by definition.
        spec = spec_from_market(
            _market(contract=False, spot=True, swap=False, contractSize=None)
        )

        assert spec.contract_size == 1.0
        assert spec.is_derivative is False


class TestFailClosed:
    """The core of #124: no metadata, no bot."""

    def test_a_derivative_without_a_contract_size_is_refused(self) -> None:
        with pytest.raises(ContractMetadataError, match="contract size"):
            spec_from_market(_market(contractSize=None))

    def test_a_derivative_with_a_zero_contract_size_is_refused(self) -> None:
        with pytest.raises(ContractMetadataError):
            spec_from_market(_market(contractSize=0))

    def test_a_derivative_that_is_neither_linear_nor_inverse_is_refused(self) -> None:
        # Ambiguity is as dangerous as absence here: the two conventions
        # differ by the price itself.
        with pytest.raises(ContractMetadataError, match="linear|inverse"):
            spec_from_market(_market(linear=None, inverse=None))

    def test_a_derivative_claiming_to_be_both_is_refused(self) -> None:
        with pytest.raises(ContractMetadataError, match="linear|inverse"):
            spec_from_market(_market(linear=True, inverse=True))

    def test_a_market_without_a_quote_currency_is_refused(self) -> None:
        with pytest.raises(ContractMetadataError):
            spec_from_market(_market(quote=None))

    def test_an_empty_market_is_refused(self) -> None:
        with pytest.raises(ContractMetadataError):
            spec_from_market({})

    def test_a_missing_tick_size_is_tolerated(self) -> None:
        # Tick size is informational; unlike contract size, nothing numeric
        # depends on it, so its absence must not block a bot.
        assert spec_from_market(_market(precision={})).tick_size is None


class _Exchange:
    """Fake ccxt client that counts metadata lookups."""

    def __init__(self, markets: dict | None = None, raises: bool = False) -> None:
        self.markets = markets if markets is not None else {"BTC/USDT:USDT": _market()}
        self.raises = raises
        self.load_calls = 0

    def load_markets(self, reload=False):
        del reload
        self.load_calls += 1
        if self.raises:
            raise RuntimeError("exchange unreachable")
        return self.markets

    def market(self, symbol):
        if symbol not in self.markets:
            raise KeyError(symbol)
        return self.markets[symbol]


class TestCache:
    def test_metadata_is_fetched_once_and_reused(self) -> None:
        exchange = _Exchange()
        cache = ContractCache(exchange, ttl_seconds=60.0, clock=lambda: 0.0)

        first = cache.spec("BTC/USDT:USDT")
        second = cache.spec("BTC/USDT:USDT")

        assert first == second
        assert exchange.load_calls == 1, "metadata must not be refetched per order"

    def test_metadata_is_refetched_after_the_ttl(self) -> None:
        # Contracts roll over and get relisted; a cache with no expiry would
        # serve last quarter's contract size indefinitely.
        exchange = _Exchange()
        now = [0.0]
        cache = ContractCache(exchange, ttl_seconds=60.0, clock=lambda: now[0])

        cache.spec("BTC/USDT:USDT")
        now[0] = 61.0
        cache.spec("BTC/USDT:USDT")

        assert exchange.load_calls == 2

    def test_the_cache_is_not_refreshed_before_the_ttl(self) -> None:
        exchange = _Exchange()
        now = [0.0]
        cache = ContractCache(exchange, ttl_seconds=60.0, clock=lambda: now[0])

        cache.spec("BTC/USDT:USDT")
        now[0] = 59.0
        cache.spec("BTC/USDT:USDT")

        assert exchange.load_calls == 1

    def test_an_unknown_symbol_is_refused(self) -> None:
        cache = ContractCache(_Exchange(), ttl_seconds=60.0, clock=lambda: 0.0)

        with pytest.raises(ContractMetadataError, match="not listed|unknown"):
            cache.spec("DOGE/USDT:USDT")

    def test_an_unreachable_exchange_is_refused_not_defaulted(self) -> None:
        cache = ContractCache(
            _Exchange(raises=True), ttl_seconds=60.0, clock=lambda: 0.0
        )

        with pytest.raises(ContractMetadataError):
            cache.spec("BTC/USDT:USDT")

    def test_a_failed_load_is_not_cached_as_success(self) -> None:
        exchange = _Exchange(raises=True)
        cache = ContractCache(exchange, ttl_seconds=60.0, clock=lambda: 0.0)

        with pytest.raises(ContractMetadataError):
            cache.spec("BTC/USDT:USDT")
        exchange.raises = False
        spec = cache.spec("BTC/USDT:USDT")

        assert isinstance(spec, ContractSpec)

    def test_refresh_forces_a_reload(self) -> None:
        """The documented escape hatch for an expiry rollover."""
        exchange = _Exchange()
        cache = ContractCache(exchange, ttl_seconds=3600.0, clock=lambda: 0.0)

        cache.spec("BTC/USDT:USDT")
        cache.refresh()
        cache.spec("BTC/USDT:USDT")

        assert exchange.load_calls == 2
