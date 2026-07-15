from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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
