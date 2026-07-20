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
    """One persisted order-lifecycle event exposed by the trades endpoint.

    Since #135 the log holds discrete lifecycle events -- a submission, a fill
    snapshot, a rejection -- rather than one flat row per order outcome. Rows
    written before that change carry no ``kind`` and are surfaced as legacy
    history rather than reinterpreted, since the information needed to
    classify them honestly was never recorded.
    """

    bot_id: str
    action: str
    status: str
    ok: bool = False
    order_id: str | None = None
    symbol: str | None = None
    ts: int | None = None
    seq: int | None = None
    """Stable per-bot cursor, used to page backward through history."""

    kind: str | None = None
    """Lifecycle event kind; ``None`` for legacy rows predating #135."""

    client_order_id: str | None = None
    """Idempotency key tying this event to its order."""

    side: str | None = None
    qty: float | None = None
    """Quantity requested, on a submission or dry run."""

    filled_qty: float | None = None
    """Cumulative quantity actually traded, on a status snapshot."""

    avg_price: float | None = None
    reason: str | None = None
    """Why an order was rejected."""

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "TradeView":
        """Build a view from a stored record, tolerating partial/legacy rows.

        Args:
            record: Raw event dict read from the store.

        Returns:
            A ``TradeView`` with missing fields left as ``None`` rather than
            guessed at. A legacy row has no ``kind``; a ledger event has no
            ``action``. Neither is coerced into the other's shape.
        """
        def _str(key: str) -> str | None:
            value = record.get(key)
            return str(value) if value is not None else None

        def _num(key: str) -> float | None:
            value = record.get(key)
            try:
                return float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None

        ts = record.get("ts")
        seq = record.get("seq")
        return cls(
            bot_id=str(record.get("bot_id", "")),
            action=str(record.get("action", "")),
            status=str(record.get("status", "")),
            ok=bool(record.get("ok", False)),
            order_id=_str("order_id") or _str("venue_order_id"),
            symbol=_str("symbol"),
            ts=int(ts) if ts is not None else None,
            seq=int(seq) if seq is not None else None,
            kind=_str("kind"),
            client_order_id=_str("client_order_id"),
            side=_str("side"),
            qty=_num("qty"),
            filled_qty=_num("filled_qty"),
            avg_price=_num("avg_price"),
            reason=_str("reason"),
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

    degraded_permanent: bool = False
    """Whether the degradation is a venue limitation a restart cannot fix."""
