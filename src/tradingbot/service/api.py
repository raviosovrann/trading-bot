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
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _load_token_hashes(store: BotStore) -> set[str]:
    data = store.load_users()
    users = data.get("users", [])
    if not isinstance(users, list):
        return set()
    return {str(u.get("token_hash", "")) for u in users if isinstance(u, dict)}


def _get_store(request: Request) -> BotStore:
    return request.app.state.store


def _get_supervisor(request: Request) -> BotSupervisor:
    return request.app.state.supervisor


async def require_auth(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
    store: BotStore = Depends(_get_store),
) -> str:
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
    if isinstance(event, DecisionEvent):
        return {"type": "decision", **asdict(event)}
    if isinstance(event, OrderEvent):
        return {"type": "order", **asdict(event)}
    return {"type": "unknown", "data": str(event)}


def _position_to_dict(position: Position | None) -> dict[str, Any] | None:
    return position.model_dump() if position is not None else None


def _to_view(bot: BotInstance) -> BotView:
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
    secrets = store.load_secrets()
    venue_secrets = secrets.get(venue)
    if not isinstance(venue_secrets, dict):
        return {}
    creds = venue_secrets.get(market_type)
    return creds if isinstance(creds, dict) else {}


def create_app(*, store: BotStore, supervisor: BotSupervisor) -> FastAPI:
    app = FastAPI(title="Trading Console")
    app.state.store = store
    app.state.supervisor = supervisor

    @app.get("/venues")
    async def list_venues(_: str = Depends(require_auth)) -> list[dict[str, str]]:
        return available_venues()

    @app.get("/strategies")
    async def list_strategies(_: str = Depends(require_auth)) -> list[str]:
        return available_strategies()

    @app.post("/bots", status_code=status.HTTP_201_CREATED)
    async def create_bot(
        request: CreateBotRequest,
        _: str = Depends(require_auth),
    ) -> BotView:
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
        return [_to_view(bot) for bot in supervisor.list()]

    @app.get("/bots/{bot_id}")
    async def get_bot(bot_id: str, _: str = Depends(require_auth)) -> BotView:
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
        bot = supervisor.get(bot_id)
        if bot is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="bot not found")
        bot.config.creds = _load_credentials(store, bot.config.venue, bot.config.market_type)
        await supervisor.start(bot_id)
        return _to_view(bot)

    @app.post("/bots/{bot_id}/stop")
    async def stop_bot(bot_id: str, _: str = Depends(require_auth)) -> BotView:
        bot = supervisor.get(bot_id)
        if bot is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="bot not found")
        await supervisor.stop(bot_id)
        return _to_view(bot)

    @app.get("/bots/{bot_id}/trades")
    async def list_trades(bot_id: str, _: str = Depends(require_auth)) -> list[dict[str, Any]]:
        if supervisor.get(bot_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="bot not found")
        return store.read_trades(bot_id)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
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
