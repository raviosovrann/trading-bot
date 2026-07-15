"""Example strategy that takes no action."""

from __future__ import annotations

from collections.abc import Sequence

from ..models import Candle, Signal
from .base import StrategyContext
from .registry import strategy


@strategy("example")
class ExampleStrategy:
    """Minimal reference strategy used to validate the plugin lifecycle."""

    def __init__(self, ctx: StrategyContext) -> None:
        """Store the runtime context for later use.

        Args:
            ctx: Strategy configuration and data feed.
        """
        self.context = ctx

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        """Return ``None`` and ignore the provided candles.

        Args:
            candles: Closed candles seen so far.

        Returns:
            Always ``None``.
        """
        del candles
        return None
