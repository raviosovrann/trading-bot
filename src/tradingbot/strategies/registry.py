from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .base import Strategy, StrategyContext

_Factory = Callable[[StrategyContext], Strategy]
_factories: dict[str, _Factory] = {}


def strategy(name: str) -> Callable[[Any], Any]:
	"""Register a strategy class or factory under ``name``."""
	if not name.strip():
		raise ValueError("strategy name must not be empty")

	def decorator(candidate: Any) -> Any:
		if name in _factories:
			raise ValueError(f"strategy {name!r} is already registered")

		def factory(ctx: StrategyContext) -> Strategy:
			return candidate(ctx)

		_factories[name] = factory
		return candidate

	return decorator


def build_strategy(name: str, ctx: StrategyContext) -> Strategy:
	try:
		factory = _factories[name]
	except KeyError as exc:
		raise ValueError(f"Unknown strategy: {name}") from exc
	return factory(ctx)


def available_strategies() -> list[str]:
	return sorted(_factories)

