from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..models import Candle, Signal


@dataclass(frozen=True)
class StrategyContext:
	symbol: str
	timeframe: str
	quantity: float
	data_feed: Any
	params: dict[str, Any]


@runtime_checkable
class Strategy(Protocol):
	def on_bar(self, candles: Sequence[Candle]) -> Signal | None: ...

