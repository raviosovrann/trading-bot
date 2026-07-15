"""FastAPI application exposing the trading console REST and WebSocket API."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import uuid
from dataclasses import asdict
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.websockets import WebSocket, WebSocketDisconnect

from ..models import Position
from .dto import BotView, CreateBotRequest, PatchBotRequest
from .events import DecisionEvent, OrderEvent
from .registry import available_strategies, available_venues
from .store import BotStore
from .supervisor import BotConfig, BotInstance, BotSupervisor

_log = logging.getLogger(__name__)
_security = HTTPBearer(auto_error=False)


def _hash_token(token: str) -> str:
    """Return the SHA-256 hex digest of ``token``."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _load_token_hashes(store: BotStore) -> set[str]:
    """Load valid bearer token hashes from ``store``.

    Args:
        store: Persistence layer containing user records.

    Returns:
        Set of configured SHA-256 token hashes.
    """
    data = store.load_users()
    users = data.get("users", [])
    if not isinstance(users, list):
        return set()
    return {str(u.get("token_hash", "")) for u in users if isinstance(u, dict)}


def _get_store(request: Request) -> BotStore:
    """Return the ``BotStore`` attached to the app state."""
    return request.app.state.store


def _get_supervisor(request: Request) -> BotSupervisor:
    """Return the ``BotSupervisor`` attached to the app state."""
    return request.app.state.supervisor


async def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
    store: BotStore = Depends(_get_store),
) -> str:
    """Validate the bearer token against stored token hashes.

    Args:
        credentials: Authorization header parsed by FastAPI.
        store: Persistence layer containing user records.

    Returns:
        The raw bearer token when authentication succeeds.

    Raises:
        HTTPException: If the token is missing, malformed or invalid.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authorization",
            headers={"WWW-Authenticate": "Bearer"},
        )
    valid_hashes = _load_token_hashes(store)
    provided_hash = _hash_token(credentials.credentials)
    if not any(hmac.compare_digest(provided_hash, h) for h in valid_hashes):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials


def _event_to_dict(event: Any) -> dict[str, Any]:
    """Serialize a supervisor event into a dictionary for the WebSocket.

    Args:
        event: Decision or order event from the event bus.

    Returns:
        Dictionary with ``type`` and event fields.
    """
    if isinstance(event, DecisionEvent):
        return {"type": "decision", **asdict(event)}
    if isinstance(event, OrderEvent):
        return {"type": "order", **asdict(event)}
    return {"type": "unknown", "data": str(event)}


def _position_to_dict(position: Position | None) -> dict[str, Any] | None:
    """Convert a ``Position`` model to a dictionary or ``None``.

    Args:
        position: Position instance, or ``None`` when flat.

    Returns:
        Dictionary representation, or ``None`` if ``position`` is ``None``.
    """
    return position.model_dump() if position is not None else None


def _to_view(bot: BotInstance) -> BotView:
    """Map a ``BotInstance`` to its API-safe view.

    Args:
        bot: Running or created bot instance.

    Returns:
        ``BotView`` without credentials.
    """
    return BotView(
        id=bot.config.id,
        venue=bot.config.venue,
        market_type=bot.config.market_type,
        strategy=bot.config.strategy,
        symbol=bot.config.symbol,
        timeframe=bot.config.timeframe,
        quantity=bot.config.quantity,
        live=bot.config.live,
        per_bot_cap=bot.config.per_bot_cap,
        global_cap=bot.config.global_cap,
        params=bot.config.params,
        status=bot.status,
        position=_position_to_dict(bot.position),
        pnl=bot.pnl,
        last_decision=bot.last_decision,
    )


def _load_credentials(store: BotStore, venue: str, market_type: str) -> dict[str, object]:
    """Load stored credentials for a venue/market-type pair.

    Args:
        store: Persistence layer containing secrets.
        venue: Venue identifier.
        market_type: Market type identifier.

    Returns:
        Credential dictionary, or an empty dictionary when none are configured.
    """
    secrets = store.load_secrets()
    venue_secrets = secrets.get(venue)
    if not isinstance(venue_secrets, dict):
        return {}
    creds = venue_secrets.get(market_type)
    return creds if isinstance(creds, dict) else {}


def create_app(*, store: BotStore, supervisor: BotSupervisor) -> FastAPI:
    """Create and configure the FastAPI trading console app.

    Args:
        store: Persistence layer attached to app state.
        supervisor: Bot supervisor attached to app state.

    Returns:
        Configured ``FastAPI`` application.
    """
    app = FastAPI(title="Trading Console")
    app.state.store = store
    app.state.supervisor = supervisor

    @app.get("/venues")
    async def list_venues(_: str = Depends(require_auth)) -> list[dict[str, str]]:
        """List supported venue/market-type mappings."""
        return available_venues()

    @app.get("/strategies")
    async def list_strategies(_: str = Depends(require_auth)) -> list[str]:
        """List registered strategy names."""
        return available_strategies()

    @app.post("/bots", status_code=status.HTTP_201_CREATED)
    async def create_bot(
        request: CreateBotRequest,
        _: str = Depends(require_auth),
    ) -> BotView:
        """Create and persist a new bot from ``request``.

        Args:
            request: Bot creation payload.

        Returns:
            API view of the newly created bot.

        Raises:
            HTTPException: If the bot cannot be retrieved after creation.
        """
        bot_id = str(uuid.uuid4())
        cfg = BotConfig(
            id=bot_id,
            venue=request.venue,
            market_type=request.market_type,
            strategy=request.strategy,
            symbol=request.symbol,
            timeframe=request.timeframe,
            quantity=request.quantity,
            live=request.live,
            per_bot_cap=request.per_bot_cap,
            global_cap=request.global_cap,
            params=request.params,
        )
        supervisor.create(cfg)
        store.save_config(cfg)
        bot = supervisor.get(bot_id)
        if bot is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="bot not found")
        return _to_view(bot)

    @app.get("/bots")
    async def list_bots(_: str = Depends(require_auth)) -> list[BotView]:
        """List all created bots."""
        return [_to_view(bot) for bot in supervisor.list()]

    @app.get("/bots/{bot_id}")
    async def get_bot(bot_id: str, _: str = Depends(require_auth)) -> BotView:
        """Return the bot identified by ``bot_id``.

        Args:
            bot_id: UUID of the bot.

        Returns:
            API view of the bot.

        Raises:
            HTTPException: If the bot does not exist.
        """
        bot = supervisor.get(bot_id)
        if bot is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="bot not found")
        return _to_view(bot)

    @app.patch("/bots/{bot_id}")
    async def patch_bot(
        bot_id: str,
        request: PatchBotRequest,
        _: str = Depends(require_auth),
    ) -> BotView:
        """Update mutable fields of the bot identified by ``bot_id``.

        Args:
            bot_id: UUID of the bot.
            request: Fields to update.

        Returns:
            API view of the updated bot.

        Raises:
            HTTPException: If the bot does not exist.
        """
        bot = supervisor.get(bot_id)
        if bot is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="bot not found")
        if request.live is not None:
            bot.config.live = request.live
        if request.per_bot_cap is not None:
            bot.config.per_bot_cap = request.per_bot_cap
        if request.global_cap is not None:
            bot.config.global_cap = request.global_cap
        if request.params is not None:
            bot.config.params = request.params
        store.save_config(bot.config)
        return _to_view(bot)

    @app.post("/bots/{bot_id}/start")
    async def start_bot(bot_id: str, _: str = Depends(require_auth)) -> BotView:
        """Start the bot identified by ``bot_id``.

        Credentials are loaded from the store and attached to the bot config
        before starting.

        Args:
            bot_id: UUID of the bot.

        Returns:
            API view of the started bot.

        Raises:
            HTTPException: If the bot does not exist or cannot be started.
        """
        bot = supervisor.get(bot_id)
        if bot is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="bot not found")
        bot.config.creds = _load_credentials(store, bot.config.venue, bot.config.market_type)
        try:
            await supervisor.start(bot_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        return _to_view(bot)

    @app.post("/bots/{bot_id}/stop")
    async def stop_bot(bot_id: str, _: str = Depends(require_auth)) -> BotView:
        """Stop the bot identified by ``bot_id``.

        Args:
            bot_id: UUID of the bot.

        Returns:
            API view of the stopped bot.

        Raises:
            HTTPException: If the bot does not exist.
        """
        bot = supervisor.get(bot_id)
        if bot is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="bot not found")
        await supervisor.stop(bot_id)
        return _to_view(bot)

    @app.get("/bots/{bot_id}/trades")
    async def list_trades(bot_id: str, _: str = Depends(require_auth)) -> list[dict[str, Any]]:
        """List persisted trade events for ``bot_id``.

        Args:
            bot_id: UUID of the bot.

        Returns:
            Trade events recorded for the bot.

        Raises:
            HTTPException: If the bot does not exist.
        """
        if supervisor.get(bot_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="bot not found")
        return store.read_trades(bot_id)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """Stream supervisor events to authenticated WebSocket clients.

        Clients must provide a valid ``token`` query parameter.

        Args:
            websocket: Accepted WebSocket connection.
        """
        await websocket.accept()
        store = websocket.app.state.store
        supervisor = websocket.app.state.supervisor
        token = websocket.query_params.get("token")
        if token is None or not any(
            hmac.compare_digest(_hash_token(token), h) for h in _load_token_hashes(store)
        ):
            await websocket.close(code=1008)
            return
        queue = supervisor.event_bus.subscribe()
        try:
            while True:
                get_task = asyncio.create_task(queue.get())
                recv_task = asyncio.create_task(websocket.receive_text())
                done, pending = await asyncio.wait(
                    {get_task, recv_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                if recv_task in done:
                    try:
                        recv_task.result()
                    except WebSocketDisconnect:
                        raise
                    # Ignore any client-to-server message and keep listening.
                    continue
                event = await get_task
                await websocket.send_json(_event_to_dict(event))
        except WebSocketDisconnect:
            pass
        finally:
            supervisor.event_bus.unsubscribe(queue)

    return app
