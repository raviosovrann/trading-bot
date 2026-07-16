"""API request/response models for the trading console."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    """Operator login payload."""

    username: str
    password: str


class LoginResponse(BaseModel):
    """Bearer token issued on successful login."""

    token: str


class CreateBotRequest(BaseModel):
    """Payload to create a new bot. No secrets are accepted here."""

    venue: str
    market_type: str
    strategy: str
    symbol: str
    timeframe: str
    quantity: float = Field(gt=0.0)
    live: bool = False
    per_bot_cap: float = Field(ge=0.0)
    global_cap: float = Field(ge=0.0)
    params: dict[str, Any] = Field(default_factory=dict)


class PatchBotRequest(BaseModel):
    """Mutable bot settings."""

    live: bool | None = None
    per_bot_cap: float | None = Field(default=None, ge=0.0)
    global_cap: float | None = Field(default=None, ge=0.0)
    params: dict[str, Any] | None = None


class TradeView(BaseModel):
    """A persisted order event exposed by the trades endpoint."""

    bot_id: str
    action: str
    status: str
    ok: bool = False
    order_id: str | None = None
    symbol: str | None = None
    ts: int | None = None

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "TradeView":
        """Build a view from a stored trade record, tolerating partial/legacy rows.

        Args:
            record: Raw trade dict read from the store.

        Returns:
            A ``TradeView`` with missing fields filled by sensible defaults.
        """
        order_id = record.get("order_id")
        symbol = record.get("symbol")
        ts = record.get("ts")
        return cls(
            bot_id=str(record.get("bot_id", "")),
            action=str(record.get("action", "")),
            status=str(record.get("status", "")),
            ok=bool(record.get("ok", False)),
            order_id=str(order_id) if order_id is not None else None,
            symbol=str(symbol) if symbol is not None else None,
            ts=int(ts) if ts is not None else None,
        )


class BotView(BaseModel):
    """Bot state exposed by the API. Credentials are never included."""

    id: str
    venue: str
    market_type: str
    strategy: str
    symbol: str
    timeframe: str
    quantity: float
    live: bool
    per_bot_cap: float
    global_cap: float
    params: dict[str, Any]
    status: str
    position: dict[str, Any] | None = None
    pnl: float
    last_decision: str | None = None
