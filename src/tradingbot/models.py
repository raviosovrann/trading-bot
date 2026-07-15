"""Shared Pydantic models for signals, orders, positions and candles."""

import math
from enum import Enum
from pydantic import BaseModel, field_validator, model_validator


class Action(str, Enum):
    """Action produced by a strategy and routed by the engine."""

    buy = "buy"
    sell = "sell"
    close = "close"


class OrderType(str, Enum):
    """Order type accepted by execution venues."""

    market = "market"
    limit = "limit"


class PositionSide(str, Enum):
    """Position side for strategy signals and venue positions."""

    long = "long"
    short = "short"
    flat = "flat"


class Signal(BaseModel):
    """Trading signal emitted by a strategy."""

    strategy: str
    """Name of the strategy that emitted the signal."""

    action: Action
    """Desired action: buy, sell or close."""

    symbol: str
    """Trading symbol."""

    order_type: OrderType = OrderType.market
    """Order type; market by default."""

    price: float | None = None
    """Limit price when ``order_type`` is ``limit``."""

    quantity: float
    """Positive order quantity."""

    position_side: PositionSide
    """Target position side context."""

    @field_validator("quantity")
    @classmethod
    def _quantity_positive(cls, v: float) -> float:
        """Ensure quantity is a finite positive number.

        Args:
            v: Raw quantity value.

        Returns:
            The validated quantity.

        Raises:
            ValueError: If ``v`` is not finite or not positive.
        """
        if not math.isfinite(v) or v <= 0:
            raise ValueError("quantity must be a finite number > 0")
        return v

    @model_validator(mode="after")
    def _limit_requires_price(self) -> "Signal":
        """Ensure limit orders provide a price.

        Returns:
            The validated signal instance.

        Raises:
            ValueError: If the order is a limit order without a price.
        """
        if self.order_type is OrderType.limit and self.price is None:
            raise ValueError("price is required for limit orders")
        return self


class Side(str, Enum):
    """Execution side for venue orders."""

    buy = "buy"
    sell = "sell"


class Candle(BaseModel):
    """A single OHLCV candle."""

    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class Order(BaseModel):
    """Order submitted to an execution venue."""

    symbol: str
    side: Side
    order_type: OrderType
    qty: float
    price: float | None = None
    reduce_only: bool = False


class OrderResult(BaseModel):
    """Result of an order submission or position close."""

    ok: bool
    order_id: str | None
    status: str
    filled_qty: float
    raw: dict
    error: str | None = None


class Position(BaseModel):
    """Open position reported by an execution venue."""

    symbol: str
    side: PositionSide
    size: float
    entry_price: float
