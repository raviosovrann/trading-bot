from __future__ import annotations

from collections.abc import Sequence

import pytest

from tradingbot.models import Candle, Signal
from tradingbot.strategies.base import Strategy, StrategyContext
from tradingbot.strategies.registry import (
    available_strategies,
    build_strategy,
    strategy,
)


class _DummyStrategy:
    def __init__(self, ctx: StrategyContext) -> None:
        self.ctx = ctx

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        del candles
        return None


class _FactoryStrategy:
    def __init__(self, ctx: StrategyContext, *, created: bool) -> None:
        self.ctx = ctx
        self.created = created

    @classmethod
    def create(cls, ctx: StrategyContext) -> _FactoryStrategy:
        return cls(ctx, created=True)

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        del candles
        return None


def test_registered_strategy_is_available_and_buildable() -> None:
    name = "test-dummy"
    strategy(name)(_DummyStrategy)
    ctx = StrategyContext(
        symbol="BTC/USD",
        timeframe="1h",
        quantity=1.0,
        data_feed=object(),
        params={"window": 5},
    )

    assert name in available_strategies()
    built = build_strategy(name, ctx)
    assert isinstance(built, _DummyStrategy)
    assert isinstance(built, Strategy)
    assert built.ctx is ctx


def test_strategy_name_is_normalized_for_registration_and_lookup() -> None:
    strategy("  test-normalized  ")(_DummyStrategy)

    assert "test-normalized" in available_strategies()
    assert isinstance(build_strategy(" test-normalized ", _context()), _DummyStrategy)


def test_strategy_can_build_via_create_factory() -> None:
    strategy("test-create")(_FactoryStrategy)

    built = build_strategy("test-create", _context())

    assert isinstance(built, _FactoryStrategy)
    assert built.created


def test_non_callable_strategy_candidate_is_rejected() -> None:
    with pytest.raises(TypeError, match="callable"):
        strategy("test-non-callable")(object())


def test_unknown_strategy_raises() -> None:
    with pytest.raises(ValueError, match="Unknown strategy"):
        build_strategy("does-not-exist", _context())


def test_empty_strategy_lookup_raises() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        build_strategy("  ", _context())


def test_duplicate_strategy_name_raises() -> None:
    name = "test-duplicate"
    strategy(name)(_DummyStrategy)

    with pytest.raises(ValueError, match="already registered"):
        strategy(name)(_DummyStrategy)


def _context() -> StrategyContext:
    return StrategyContext(
        symbol="BTC/USD",
        timeframe="1m",
        quantity=0.1,
        data_feed=None,
        params={},
    )
