import math
from enum import Enum
from pydantic import BaseModel, field_validator, model_validator


class Action(str, Enum):
    buy = "buy"
    sell = "sell"
    close = "close"


class OrderType(str, Enum):
    market = "market"
    limit = "limit"


class PositionSide(str, Enum):
    long = "long"
    short = "short"
    flat = "flat"


class Signal(BaseModel):
    strategy: str
    action: Action
    symbol: str
    order_type: OrderType = OrderType.market
    price: float | None = None
    quantity: float
    position_side: PositionSide

    @field_validator("quantity")
    @classmethod
    def _quantity_positive(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0:
            raise ValueError("quantity must be a finite number > 0")
        return v

    @model_validator(mode="after")
    def _limit_requires_price(self) -> "Signal":
        if self.order_type is OrderType.limit and self.price is None:
            raise ValueError("price is required for limit orders")
        return self


class Side(str, Enum):
    buy = "buy"
    sell = "sell"


class Candle(BaseModel):
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class Order(BaseModel):
    symbol: str
    side: Side
    order_type: OrderType
    qty: float
    price: float | None = None
    reduce_only: bool = False


class OrderResult(BaseModel):
    ok: bool
    order_id: str | None
    status: str
    filled_qty: float
    raw: dict
    error: str | None = None


class Position(BaseModel):
    symbol: str
    side: str  # "long" | "short" | "flat"
    size: float
    entry_price: float
