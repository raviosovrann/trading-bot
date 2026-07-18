"""FastAPI application exposing the trading console REST and WebSocket API."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import uuid
from dataclasses import asdict
from typing import Any

from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from fastapi.websockets import WebSocket, WebSocketDisconnect

from ..models import Position
from .auth import hash_password, verify_password
from .dto import BotView, CreateBotRequest, LoginRequest, LoginResponse, PatchBotRequest, TradeView
from .events import DecisionEvent, OrderEvent
from .registry import available_strategies, available_venues
from .store import BotStore
from .supervisor import BotConfig, BotInstance, BotSupervisor

_log = logging.getLogger(__name__)
_security = HTTPBearer(auto_error=False)

# A valid PBKDF2 encoding verified against when the username is unknown, so login
# always performs the same hashing work and does not leak which usernames exist.
_DUMMY_PASSWORD_HASH = hash_password("")


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


def create_app(
    *, store: BotStore, supervisor: BotSupervisor, spa_dir: Path | None = None
) -> FastAPI:
    """Create and configure the FastAPI trading console app.

    Args:
        store: Persistence layer attached to app state.
        supervisor: Bot supervisor attached to app state.
        spa_dir: Optional directory of a built SPA to serve at ``/``. When given
            and it contains ``index.html``, the SPA is served with client-side
            routing fallback; otherwise only the API is served.

    Returns:
        Configured ``FastAPI`` application.
    """
    app = FastAPI(title="Trading Console")
    app.state.store = store
    app.state.supervisor = supervisor

    # REST routes live under /api so the SPA can be mounted at / without shadowing
    # them. The WebSocket stays at /ws (top-level, simpler to proxy).
    api_router = APIRouter(prefix="/api")

    @api_router.post("/login")
    async def login(request: LoginRequest) -> LoginResponse:
        """Authenticate an operator and mint a fresh bearer token.

        On success a new random token is issued and the user's stored token
        hash is rotated to match, so only the SHA-256 hash is ever persisted and
        the previous token is invalidated. Unknown users still run a password
        verification against a dummy hash to avoid leaking which usernames exist.

        Args:
            request: Username and password payload.

        Returns:
            The newly minted bearer token.

        Raises:
            HTTPException: 401 when the username or password is invalid.
        """
        data = store.load_users()
        users = data.get("users", []) if isinstance(data, dict) else []
        user = next(
            (u for u in users if isinstance(u, dict) and u.get("username") == request.username),
            None,
        )
        stored_hash = str(user.get("password_hash", "")) if user is not None else _DUMMY_PASSWORD_HASH
        if not verify_password(request.password, stored_hash) or user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password",
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = secrets.token_urlsafe(32)
        user["token_hash"] = _hash_token(token)
        store.save_users(data)
        return LoginResponse(token=token)

    @api_router.put(
        "/venues/{venue}/{market_type}/secrets",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        response_model=None,
    )
    async def put_secrets(
        venue: str,
        market_type: str,
        creds: dict[str, str],
        _: str = Depends(require_auth),
    ) -> None:
        """Store venue credentials for a ``(venue, market_type)`` pair.

        The credentials are persisted server-side only and are never echoed back
        or logged. ``start_bot`` later loads them from the store.

        Args:
            venue: Venue identifier, e.g. ``coinbase``.
            market_type: Market type identifier, e.g. ``spot`` or ``futures``.
            creds: Credential mapping (shape depends on the venue).

        Raises:
            HTTPException: 400 when no credentials are supplied.
        """
        if not creds:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="no credentials supplied",
            )
        store.save_secrets(venue, market_type, creds)

    @api_router.get("/venues")
    async def list_venues(_: str = Depends(require_auth)) -> list[dict[str, str]]:
        """List supported venue/market-type mappings."""
        return available_venues()

    @api_router.get("/strategies")
    async def list_strategies(_: str = Depends(require_auth)) -> list[str]:
        """List registered strategy names."""
        return available_strategies()

    @api_router.post("/bots", status_code=status.HTTP_201_CREATED)
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

    @api_router.get("/bots")
    async def list_bots(_: str = Depends(require_auth)) -> list[BotView]:
        """List all created bots."""
        return [_to_view(bot) for bot in supervisor.list()]

    @api_router.get("/bots/{bot_id}")
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

    @api_router.patch("/bots/{bot_id}")
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

    @api_router.post("/bots/{bot_id}/start")
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

    @api_router.post("/bots/{bot_id}/stop")
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

    @api_router.get("/bots/{bot_id}/trades")
    async def list_trades(bot_id: str, _: str = Depends(require_auth)) -> list[TradeView]:
        """List persisted trade events for ``bot_id`` as typed views.

        Args:
            bot_id: UUID of the bot.

        Returns:
            Trade events recorded for the bot.

        Raises:
            HTTPException: If the bot does not exist.
        """
        if supervisor.get(bot_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="bot not found")
        return [TradeView.from_record(record) for record in store.read_trades(bot_id)]

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

    app.include_router(api_router)
    if spa_dir is not None:
        _mount_spa(app, spa_dir)
    return app


def _mount_spa(app: FastAPI, dist: Path) -> None:
    """Serve the built SPA from ``dist`` when it contains ``index.html``.

    Static assets are served from ``/assets``; every other non-API path falls
    back to ``index.html`` so client-side routing (deep links, refresh) works.
    When the bundle hasn't been built, this is a no-op so the API still runs.

    Args:
        app: The FastAPI application to attach the SPA routes to.
        dist: Directory containing the built SPA (``index.html`` + ``assets/``).
    """
    # Resolve the root once so every file candidate can be checked against the
    # canonical directory.  The catch-all receives percent-decoded paths, so a
    # lexical ``dist / full_path`` check is not sufficient to stop ``..`` or a
    # symlink inside the bundle from escaping the SPA directory.
    root = dist.resolve()
    index = root / "index.html"
    if not index.is_file():
        return
    assets = root / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/{full_path:path}")
    async def spa(full_path: str) -> FileResponse:
        """Return a static file if it exists, else the SPA entry point."""
        safe_path = full_path.lstrip("/\\")
        candidate = (root / safe_path).resolve()
        if not candidate.is_relative_to(root):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        if safe_path and candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(index))
