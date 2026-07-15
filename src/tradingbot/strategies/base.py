"""Strategy protocol and context used by the trading runtime."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..models import Candle, Signal


@dataclass(frozen=True)
class StrategyContext:
    """Runtime configuration supplied to every strategy instance."""

    symbol: str
    """Trading symbol, e.g. ``BTC/USD``."""

    timeframe: str
    """Candle timeframe, e.g. ``1h``."""

    quantity: float
    """Default order quantity for the strategy."""

    data_feed: Any
    """Feed instance the strategy can query for warm-up candles."""

    params: dict[str, Any]
    """Strategy-specific parameters from the bot configuration."""


@runtime_checkable
class Strategy(Protocol):
    """Protocol implemented by every trade strategy."""

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        """Generate a trading signal from the latest closed bar(s).

        Args:
            candles: Closed candles seen so far, ordered oldest-first.

        Returns:
            A signal to route, or ``None`` when no action is taken.
        """
        ...

