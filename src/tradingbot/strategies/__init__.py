from __future__ import annotations

from importlib import import_module
from pkgutil import iter_modules

from .base import Strategy, StrategyContext
from .registry import available_strategies, build_strategy, strategy


def _discover() -> None:
    for module in iter_modules(__path__):
        if module.name.startswith("_"):
            continue
        import_module(f"{__name__}.{module.name}")


_discover()

__all__ = [
    "Strategy",
    "StrategyContext",
    "available_strategies",
    "build_strategy",
    "strategy",
]
