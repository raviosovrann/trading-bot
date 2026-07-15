from __future__ import annotations

from collections.abc import Sequence

from ..models import Candle, Signal
from .base import StrategyContext
from .registry import strategy


@strategy("example")
class ExampleStrategy:
    """Minimal reference strategy used to validate the plugin lifecycle."""

    def __init__(self, ctx: StrategyContext) -> None:
        self.context = ctx

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        del candles
        return None
