from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from .base import Strategy, StrategyContext

_Factory = Callable[[StrategyContext], Strategy]
_factories: dict[str, _Factory] = {}


def strategy(name: str) -> Callable[[Any], Any]:
    """Register a strategy class or factory under ``name``."""
    normalized_name = name.strip()
    if not normalized_name:
        raise ValueError("strategy name must not be empty")

    def decorator(candidate: Any) -> Any:
        if not callable(candidate) and not callable(getattr(candidate, "create", None)):
            raise TypeError("strategy candidate must be callable or expose callable create(ctx)")
        if normalized_name in _factories:
            raise ValueError(f"strategy {normalized_name!r} is already registered")

        def factory(ctx: StrategyContext) -> Strategy:
            create = getattr(candidate, "create", None)
            if callable(create):
                return cast(Strategy, create(ctx))
            return cast(Strategy, candidate(ctx))

        _factories[normalized_name] = factory
        return candidate

    return decorator


def build_strategy(name: str, ctx: StrategyContext) -> Strategy:
    normalized_name = name.strip()
    if not normalized_name:
        raise ValueError("strategy name must not be empty")
    try:
        factory = _factories[normalized_name]
    except KeyError as exc:
        raise ValueError(f"Unknown strategy: {normalized_name}") from exc
    return factory(ctx)


def available_strategies() -> list[str]:
    return sorted(_factories)

