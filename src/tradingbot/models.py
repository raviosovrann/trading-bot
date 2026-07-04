import math
from enum import Enum
from pydantic import BaseModel, field_validator


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
    token: str
    strategy: str
    action: Action
    symbol: str
    order_type: OrderType = OrderType.market
    price: float | None = None
    quantity: float
    position_side: PositionSide
    time: str | None = None

    @field_validator("quantity")
    @classmethod
    def _quantity_positive(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0:
            raise ValueError("quantity must be a finite number > 0")
        return v
