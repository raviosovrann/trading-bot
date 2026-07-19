"""API request/response models for the trading console."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


TRADES_MAX_PAGE = 500
"""Server-enforced ceiling on a single page of trade history."""


class LoginRequest(BaseModel):
    """Operator login payload."""

    username: str
    password: str


class SessionInfo(BaseModel):
    """The authenticated user's display info returned by login/session.

    No secret is included: the browser session lives in an HttpOnly cookie, so
    the SPA only needs the username and roles to render authenticated state.
    """

    username: str
    roles: list[str] = Field(default_factory=list)


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
    seq: int | None = None
    """Stable per-bot cursor, used to page backward through history."""

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
        seq = record.get("seq")
        return cls(
            bot_id=str(record.get("bot_id", "")),
            action=str(record.get("action", "")),
            status=str(record.get("status", "")),
            ok=bool(record.get("ok", False)),
            order_id=str(order_id) if order_id is not None else None,
            symbol=str(symbol) if symbol is not None else None,
            ts=int(ts) if ts is not None else None,
            seq=int(seq) if seq is not None else None,
        )


class TradePage(BaseModel):
    """One page of trade history, newest first.

    History grows without bound, so the API never returns all of it. Callers
    follow ``next_cursor`` backward until it is ``None``.
    """

    items: list[TradeView]
    next_cursor: int | None = None
    """``seq`` to pass as ``before`` for the next page, or ``None`` when done."""


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
    degraded: bool = False
    """Whether the bot is running but no longer receiving market data."""

    degraded_reason: str | None = None
    """Why the bot is degraded, or ``None`` when healthy."""
