"""FastAPI application exposing the trading console REST and WebSocket API."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from fastapi.websockets import WebSocket, WebSocketDisconnect

from ..models import Position
from ..stream import StreamingNotSupported
from .audit import AuditLog
from .auth import hash_password, needs_rehash, verify_password
from .dto import (
    TRADES_MAX_PAGE,
    BotView,
    CreateBotRequest,
    LoginRequest,
    PatchBotRequest,
    SessionInfo,
    TradePage,
    TradeView,
)
from .events import BotStateEvent, DecisionEvent, OrderEvent, OverflowEvent
from .health import readiness
from .login_guard import LoginGuard, LoginLocked
from .principal import Principal
from .registry import available_strategies, available_venues
from .sessions import SessionStore
from .store import BotStore
from .supervisor import BotConfig, BotInstance, BotSupervisor

_log = logging.getLogger(__name__)
_security = HTTPBearer(auto_error=False)

# Cookie/header names for the browser session. The session id is HttpOnly; the
# CSRF token is a readable companion cookie the SPA echoes back in a header.
SESSION_COOKIE = "tb_session"
CSRF_COOKIE = "tb_csrf"
CSRF_HEADER = "x-csrf-token"

# A valid PBKDF2 encoding verified against when the username is unknown, so login
# always performs the same hashing work and does not leak which usernames exist.
_DUMMY_PASSWORD_HASH = hash_password("")


def _hash_token(token: str) -> str:
    """Return the SHA-256 hex digest of ``token``."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _load_users(store: BotStore) -> list[dict[str, Any]]:
    """Return the list of user records from ``store``."""
    data = store.load_users()
    users = data.get("users", []) if isinstance(data, dict) else []
    return [u for u in users if isinstance(u, dict)] if isinstance(users, list) else []


def _roles_of(user: dict[str, Any]) -> tuple[str, ...]:
    """Return the roles for ``user``, defaulting to ``("operator",)``."""
    roles = user.get("roles")
    if isinstance(roles, list) and roles:
        return tuple(str(r) for r in roles)
    return ("operator",)


def _principal_of(user: dict[str, Any], kind: str) -> Principal:
    """Build a :class:`Principal` from a stored user record.

    Args:
        user: Stored user mapping.
        kind: ``"user"`` (session) or ``"service"`` (direct-API token).

    Returns:
        The resolved principal. ``id`` falls back to the username for records
        created before stable ids existed.
    """
    username = str(user.get("username", ""))
    return Principal(
        id=str(user.get("id") or username),
        username=username,
        roles=_roles_of(user),
        kind=kind,
    )


def _ensure_user_id(store: BotStore, user: dict[str, Any]) -> str:
    """Return ``user``'s stable id, assigning and persisting one if absent.

    Records created before stable ids existed are upgraded in place the first
    time they authenticate, so sessions and audit records reference a durable id.
    """
    existing = user.get("id")
    if existing:
        return str(existing)
    new_id = str(uuid.uuid4())
    store.update_user(str(user.get("username", "")), updates={"id": new_id})
    return new_id


def _resolve_token_principal(store: BotStore, token: str) -> Principal | None:
    """Resolve a direct-API bearer ``token`` to a service principal, or ``None``."""
    provided = _hash_token(token)
    for user in _load_users(store):
        stored = str(user.get("token_hash", ""))
        if stored and hmac.compare_digest(provided, stored):
            return _principal_of(user, kind="service")
    return None


def _get_store(request: Request) -> BotStore:
    """Return the ``BotStore`` attached to the app state."""
    return request.app.state.store


def _get_sessions(request: Request) -> SessionStore:
    """Return the ``SessionStore`` attached to the app state."""
    return request.app.state.sessions


def _get_supervisor(request: Request) -> BotSupervisor:
    """Return the ``BotSupervisor`` attached to the app state."""
    return request.app.state.supervisor


def _unauthorized() -> HTTPException:
    """Return a generic 401 that does not leak why authentication failed."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _resolve_cookie_principal(
    request: Request, store: BotStore, sessions: SessionStore
) -> Principal | None:
    """Resolve the session cookie to a user principal, or ``None``.

    Records the live session on ``request.state`` so CSRF validation can read the
    session's expected token without re-resolving it.
    """
    session = sessions.resolve(request.cookies.get(SESSION_COOKIE))
    if session is None:
        return None
    user = next((u for u in _load_users(store) if str(u.get("id") or u.get("username")) == session.user_id), None)
    if user is None or user.get("disabled"):
        return None
    request.state.session = session
    return _principal_of(user, kind="user")


async def require_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
    store: BotStore = Depends(_get_store),
    sessions: SessionStore = Depends(_get_sessions),
) -> Principal:
    """Authenticate a request via session cookie or direct-API bearer token.

    Browser clients present the ``tb_session`` cookie; scripts present an
    ``Authorization: Bearer`` API token. Either resolves to a :class:`Principal`.

    Args:
        request: The incoming request (carries cookies and per-request state).
        credentials: Authorization header parsed by FastAPI, when present.
        store: Persistence layer containing user records.
        sessions: Server-side session store.

    Returns:
        The authenticated principal.

    Raises:
        HTTPException: 401 when neither credential authenticates.
    """
    principal = _resolve_cookie_principal(request, store, sessions)
    if principal is not None:
        return principal
    if credentials is not None and credentials.scheme.lower() == "bearer":
        principal = _resolve_token_principal(store, credentials.credentials)
        if principal is not None:
            return principal
    raise _unauthorized()


async def require_auth_csrf(
    request: Request,
    principal: Principal = Depends(require_auth),
) -> Principal:
    """Authenticate, and enforce CSRF for cookie-authenticated state changes.

    Cookie sessions are ambient, so a state-changing request authenticated by
    cookie must also echo the session CSRF token in the ``X-CSRF-Token`` header
    (double-submit). Direct-API bearer callers carry no ambient cookie and are
    exempt.

    Args:
        request: The incoming request; ``request.state.session`` is set when the
            caller authenticated via cookie.
        principal: The already-authenticated principal.

    Returns:
        The authenticated principal.

    Raises:
        HTTPException: 403 when a cookie session fails CSRF validation.
    """
    session = getattr(request.state, "session", None)
    if session is None:  # bearer/API caller — no ambient cookie, no CSRF risk
        return principal
    header = request.headers.get(CSRF_HEADER, "")
    if not header or not hmac.compare_digest(header, session.csrf_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing or invalid CSRF token",
        )
    return principal


async def require_admin(principal: Principal = Depends(require_auth)) -> Principal:
    """Authenticate and require the ``admin`` role (read-only; no CSRF).

    Args:
        principal: The authenticated principal.

    Returns:
        The principal when it holds the ``admin`` role.

    Raises:
        HTTPException: 403 when the principal is not an admin.
    """
    if not principal.has_role("admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
    return principal


def _audit(
    request: Request,
    principal: Principal | None,
    action: str,
    target: str,
    outcome: str,
    *,
    before: Any = None,
    after: Any = None,
) -> None:
    """Write one audit record for the current request.

    Args:
        request: The request (carries the correlation id and the audit log).
        principal: The acting principal, or ``None`` for pre-auth events.
        action: Dotted action name.
        target: Affected resource identifier.
        outcome: ``"success"``, ``"failure"``, or ``"denied"``.
        before: Optional prior state (redacted before storage).
        after: Optional new state (redacted before storage).
    """
    request.app.state.audit.record(
        actor=principal,
        action=action,
        target=target,
        request_id=getattr(request.state, "request_id", ""),
        outcome=outcome,
        before=before,
        after=after,
    )


def _allowed_origins() -> set[str]:
    """Return the configured WebSocket origin allowlist from the environment."""
    raw = os.environ.get("TRADINGBOT_ALLOWED_ORIGINS", "")
    return {o.strip() for o in raw.split(",") if o.strip()}


def _origin_allowed(origin: str | None, host: str | None) -> bool:
    """Return whether a WebSocket ``origin`` may connect.

    A configured allowlist (``TRADINGBOT_ALLOWED_ORIGINS``) is authoritative.
    With no allowlist, same-origin connections are accepted by comparing the
    Origin's host to the request ``Host`` header. A missing Origin is allowed:
    browsers always send it (so cross-site attempts are still rejected), while
    non-browser clients that omit it cannot mount a CSRF-style attack.
    """
    if origin is None:
        return True
    allowed = _allowed_origins()
    if allowed:
        return origin in allowed
    if host is None:
        return False
    return origin.split("://", 1)[-1] == host


def _cookie_secure(request: Request) -> bool:
    """Return whether session cookies should carry the ``Secure`` flag.

    ``TRADINGBOT_COOKIE_SECURE`` forces the flag on/off; otherwise it tracks the
    request scheme (``https`` behind a TLS-terminating proxy with forwarded
    headers), so cookies work over plain HTTP in local development and tests.
    """
    override = os.environ.get("TRADINGBOT_COOKIE_SECURE", "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        return True
    if override in ("0", "false", "no", "off"):
        return False
    return request.url.scheme == "https"


def _set_session_cookies(
    response: Response, request: Request, raw_id: str, csrf_token: str
) -> None:
    """Attach the HttpOnly session cookie and readable CSRF cookie to ``response``."""
    secure = _cookie_secure(request)
    response.set_cookie(
        SESSION_COOKIE, raw_id,
        httponly=True, secure=secure, samesite="strict", path="/",
    )
    response.set_cookie(
        CSRF_COOKIE, csrf_token,
        httponly=False, secure=secure, samesite="strict", path="/",
    )


def _clear_session_cookies(response: Response) -> None:
    """Remove the session and CSRF cookies from the browser."""
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


def _event_to_dict(event: Any) -> dict[str, Any]:
    """Serialize a supervisor event into a dictionary for the WebSocket.

    Args:
        event: State, decision or order event from the event bus.

    Returns:
        Dictionary with ``type`` and event fields.
    """
    if isinstance(event, BotStateEvent):
        return {"type": "state", **asdict(event)}
    if isinstance(event, OverflowEvent):
        return {"type": "overflow", **asdict(event)}
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
        degraded=bot.degraded,
        degraded_reason=bot.degraded_reason,
        degraded_permanent=bot.degraded_permanent,
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
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Restore persisted bots on startup and stop them all on the way out.

        Bots are adopted from the store in a non-running state, so a restart
        never silently resumes trading — the operator sees their bots again and
        starts the ones they want.

        SIGTERM reaches uvicorn, which runs this shutdown phase, so bots and
        their market-data streams are closed cleanly instead of being killed
        mid-order.
        """
        try:
            supervisor.restore()
        except Exception:  # noqa: BLE001 - a bad store must not stop the service booting
            _log.exception("failed to restore persisted bots")
        yield
        for bot in supervisor.list():
            try:
                await supervisor.stop(bot.config.id)
            except Exception:  # noqa: BLE001 - never block shutdown on one bot
                _log.exception("failed to stop bot %s during shutdown", bot.config.id)
        # Release the venue worker threads (#111). Not waited on: a pool may
        # be parked in a hung exchange call, and shutdown must not inherit it.
        supervisor.shutdown_workers()

    app = FastAPI(title="Trading Console", lifespan=lifespan)
    app.state.store = store
    app.state.supervisor = supervisor
    app.state.sessions = SessionStore(store)
    app.state.login_guard = LoginGuard()
    app.state.audit = AuditLog(store)

    # Probes live at the top level (before the SPA catch-all is mounted, so they
    # are not shadowed) and are unauthenticated: an orchestrator must be able to
    # reach them without credentials. Neither leaks configuration values.
    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Shallow liveness probe: the process is up and serving."""
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(response: Response) -> dict[str, Any]:
        """Dependency-aware readiness probe.

        Returns 503 until the data directory is writable and the secrets key
        decrypts stored secrets, so traffic is never routed to an instance that
        cannot persist trades or read credentials.
        """
        ready, checks = readiness(store)
        response.status_code = status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE
        return {"ready": ready, "checks": checks}

    @app.middleware("http")
    async def _request_id(request: Request, call_next: Any) -> Any:
        """Attach a correlation id to each request and echo it back."""
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    # REST routes live under /api so the SPA can be mounted at / without shadowing
    # them. The WebSocket stays at /ws (top-level, simpler to proxy).
    api_router = APIRouter(prefix="/api")

    @api_router.post("/login")
    async def login(
        body: LoginRequest, request: Request, response: Response
    ) -> SessionInfo:
        """Authenticate an operator and open a browser session.

        On success a server-side session is created and its random id is set in a
        ``Secure; HttpOnly; SameSite=Strict`` cookie (plus a readable CSRF
        companion cookie); no secret is returned in the body. Unknown or disabled
        users still run a password verification against a dummy hash so login
        timing does not leak which usernames exist.

        Args:
            body: Username and password payload.
            request: The incoming request (for cookie scheme).
            response: Response whose cookies carry the new session.

        Returns:
            The authenticated user's display info.

        Raises:
            HTTPException: 401 when the username or password is invalid; 429 when
                the username or client IP is temporarily locked out.
        """
        client_ip = request.client.host if request.client else "unknown"
        target = f"user:{body.username}"
        guard = app.state.login_guard
        try:
            guard.check(f"user:{body.username}", f"ip:{client_ip}")
        except LoginLocked as exc:
            _audit(request, None, "login", target, "denied", after={"reason": "locked_out"})
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many failed attempts; try again later",
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc
        user = next(
            (u for u in _load_users(store) if u.get("username") == body.username),
            None,
        )
        stored_hash = str(user.get("password_hash", "")) if user is not None else _DUMMY_PASSWORD_HASH
        password_ok = verify_password(body.password, stored_hash)
        if not password_ok or user is None or user.get("disabled"):
            guard.record_failure(f"user:{body.username}", f"ip:{client_ip}")
            _audit(request, None, "login", target, "failure")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password",
                headers={"WWW-Authenticate": "Bearer"},
            )
        guard.record_success(f"user:{body.username}", f"ip:{client_ip}")
        # Transparently upgrade a stale password hash now that we hold the plaintext.
        if needs_rehash(stored_hash):
            store.update_user(
                str(user.get("username", "")),
                updates={"password_hash": hash_password(body.password)},
            )
        user_id = _ensure_user_id(store, user)
        raw_id, csrf_token = app.state.sessions.create(user_id)
        _set_session_cookies(response, request, raw_id, csrf_token)
        _audit(request, _principal_of(user, kind="user"), "login", target, "success")
        return SessionInfo(username=str(user.get("username", "")), roles=list(_roles_of(user)))

    @api_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
    async def logout(request: Request, response: Response) -> Response:
        """Revoke the current browser session and clear its cookies.

        Guarded by double-submit CSRF against the readable ``tb_csrf`` cookie so a
        cross-site page cannot force a logout, yet it still works for an expired
        session (the cookie outlives the server-side record), letting a stale
        browser reach a clean logged-out state.

        Raises:
            HTTPException: 403 when the CSRF token is missing or mismatched.
        """
        cookie_csrf = request.cookies.get(CSRF_COOKIE, "")
        header_csrf = request.headers.get(CSRF_HEADER, "")
        if not cookie_csrf or not header_csrf or not hmac.compare_digest(cookie_csrf, header_csrf):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Missing or invalid CSRF token",
            )
        principal = _resolve_cookie_principal(request, store, app.state.sessions)
        app.state.sessions.revoke(request.cookies.get(SESSION_COOKIE))
        _clear_session_cookies(response)
        _audit(request, principal, "logout", "session", "success")
        response.status_code = status.HTTP_204_NO_CONTENT
        return response

    @api_router.get("/audit")
    async def get_audit(
        request: Request,
        limit: int = 50,
        before: int | None = None,
        _: Principal = Depends(require_admin),
    ) -> dict[str, Any]:
        """Return a page of audit records, newest first (admin only).

        Args:
            request: The incoming request.
            limit: Maximum records per page (1–500).
            before: Cursor — return records with ``seq`` below this value.

        Returns:
            ``{"events": [...], "next_cursor": <seq|null>, "chain_ok": <bool>}``.
        """
        del request
        limit = max(1, min(limit, 500))
        events, next_cursor = store.read_audit(limit=limit, before_seq=before)
        return {"events": events, "next_cursor": next_cursor, "chain_ok": store.verify_audit_chain()}

    @api_router.get("/session")
    async def session_info(principal: Principal = Depends(require_auth)) -> SessionInfo:
        """Return the authenticated user's info so the SPA can restore its state.

        Requires a valid session (or API token); returns 401 otherwise, letting
        the SPA distinguish logged-in from logged-out without holding a secret.
        """
        return SessionInfo(username=principal.username, roles=list(principal.roles))

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
        http_request: Request,
        principal: Principal = Depends(require_auth_csrf),
    ) -> None:
        """Store venue credentials for a ``(venue, market_type)`` pair.

        The credentials are persisted server-side only and are never echoed back
        or logged. ``start_bot`` later loads them from the store.

        Args:
            venue: Venue identifier, e.g. ``coinbase``.
            market_type: Market type identifier, e.g. ``spot`` or ``futures``.
            creds: Credential mapping (shape depends on the venue).
            http_request: The incoming request (for audit correlation).

        Raises:
            HTTPException: 400 when no credentials are supplied.
        """
        if not creds:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="no credentials supplied",
            )
        # The venue client is built once when a bot starts, so rotating under a
        # running bot would leave the API advertising credentials that bot is
        # not using — the same half-applied state #109 refuses for config.
        # Refuse rather than restart: an automatic restart mid-position could
        # re-enter the market on the operator's behalf.
        active = [
            bot.config.id
            for bot in supervisor.list()
            if bot.config.venue.strip().lower() == venue.strip().lower()
            and bot.config.market_type.strip().lower() == market_type.strip().lower()
            and bot.status not in ("created", "stopped", "failed")
        ]
        if active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "cannot rotate credentials while bots are running on "
                    f"{venue}/{market_type}: {', '.join(sorted(active))}. "
                    "Stop them, rotate, then start again."
                ),
            )
        store.save_secrets(venue, market_type, creds)
        # Drop the cached clients eagerly. Waiting for the next start would
        # leave the superseded socket reconnecting on a revoked key.
        invalidate = getattr(supervisor.hub_factory, "invalidate", None)
        if callable(invalidate):
            invalidate(venue.strip().lower(), market_type.strip().lower())
        _audit(
            http_request, principal, "credentials.update",
            f"venue:{venue}/{market_type}", "success",
            after={"fields": sorted(creds)},  # key names only; values never logged
        )

    @api_router.get("/venues")
    async def list_venues(_: Principal = Depends(require_auth)) -> list[dict[str, str]]:
        """List supported venue/market-type mappings."""
        return available_venues()

    @api_router.get("/strategies")
    async def list_strategies(_: Principal = Depends(require_auth)) -> list[str]:
        """List registered strategy names."""
        return available_strategies()

    @api_router.post("/bots", status_code=status.HTTP_201_CREATED)
    async def create_bot(
        request: CreateBotRequest,
        http_request: Request,
        principal: Principal = Depends(require_auth_csrf),
    ) -> BotView:
        """Create and persist a new bot from ``request``.

        Args:
            request: Bot creation payload.
            http_request: The incoming request (for audit correlation).

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
        _audit(
            http_request, principal, "bot.create", f"bot:{bot_id}", "success",
            after={"venue": cfg.venue, "market_type": cfg.market_type, "strategy": cfg.strategy,
                   "symbol": cfg.symbol, "live": cfg.live, "per_bot_cap": cfg.per_bot_cap,
                   "global_cap": cfg.global_cap},
        )
        return _to_view(bot)

    @api_router.get("/bots")
    async def list_bots(_: Principal = Depends(require_auth)) -> list[BotView]:
        """List all created bots."""
        return [_to_view(bot) for bot in supervisor.list()]

    @api_router.get("/bots/{bot_id}")
    async def get_bot(bot_id: str, _: Principal = Depends(require_auth)) -> BotView:
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
        http_request: Request,
        principal: Principal = Depends(require_auth_csrf),
    ) -> BotView:
        """Update mutable fields of the bot identified by ``bot_id``.

        Args:
            bot_id: UUID of the bot.
            request: Fields to update.
            http_request: The incoming request (for audit correlation).

        Returns:
            API view of the updated bot.

        Raises:
            HTTPException: If the bot does not exist.
        """
        bot = supervisor.get(bot_id)
        if bot is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="bot not found")
        # Every field here is runtime-affecting: the venue (live vs dry-run),
        # the risk guard (caps) and the strategy (params) are all constructed
        # once in BotSupervisor.start() from this config. Mutating it while the
        # bot is running or mid-transition would change only the advertised
        # value while the live objects kept their original behaviour — a LIVE
        # toggle that does nothing is the most dangerous form of that. So the
        # config is immutable unless the bot is stopped: stop, edit, start.
        if bot.status in ("running", "starting", "stopping"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"bot is {bot.status}; configuration can only be changed while stopped. "
                    "Stop the bot, apply the change, then start it again."
                ),
            )
        before = {
            "live": bot.config.live,
            "per_bot_cap": bot.config.per_bot_cap,
            "global_cap": bot.config.global_cap,
        }
        if request.live is not None:
            bot.config.live = request.live
        if request.per_bot_cap is not None:
            bot.config.per_bot_cap = request.per_bot_cap
        if request.global_cap is not None:
            bot.config.global_cap = request.global_cap
        if request.params is not None:
            bot.config.params = request.params
        store.save_config(bot.config)
        after = {
            "live": bot.config.live,
            "per_bot_cap": bot.config.per_bot_cap,
            "global_cap": bot.config.global_cap,
        }
        # A live-mode flip is a distinct safety-critical action; caps are risk policy.
        action = "bot.live_mode" if before["live"] != after["live"] else "bot.update"
        _audit(http_request, principal, action, f"bot:{bot_id}", "success", before=before, after=after)
        return _to_view(bot)

    @api_router.post("/bots/{bot_id}/start")
    async def start_bot(
        bot_id: str,
        http_request: Request,
        principal: Principal = Depends(require_auth_csrf),
    ) -> BotView:
        """Start the bot identified by ``bot_id``.

        Credentials are loaded from the store and attached to the bot config
        before starting.

        Args:
            bot_id: UUID of the bot.
            http_request: The incoming request (for audit correlation).

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
        except (ValueError, StreamingNotSupported) as exc:
            # StreamingNotSupported is an operator-actionable configuration
            # problem (this venue cannot stream this market), not a server
            # fault — surfacing it as a 500 would hide the one detail that
            # tells them what to change.
            _audit(
                http_request, principal, "bot.start", f"bot:{bot_id}", "failure",
                after={"error": str(exc)},
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        _audit(
            http_request, principal, "bot.start", f"bot:{bot_id}", "success",
            after={"live": bot.config.live},
        )
        return _to_view(bot)

    @api_router.post("/bots/{bot_id}/stop")
    async def stop_bot(
        bot_id: str,
        http_request: Request,
        principal: Principal = Depends(require_auth_csrf),
    ) -> BotView:
        """Stop the bot identified by ``bot_id``.

        Args:
            bot_id: UUID of the bot.
            http_request: The incoming request (for audit correlation).

        Returns:
            API view of the stopped bot.

        Raises:
            HTTPException: If the bot does not exist.
        """
        bot = supervisor.get(bot_id)
        if bot is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="bot not found")
        await supervisor.stop(bot_id)
        _audit(http_request, principal, "bot.stop", f"bot:{bot_id}", "success")
        return _to_view(bot)

    @api_router.get("/bots/{bot_id}/trades")
    async def list_trades(
        bot_id: str,
        limit: int = Query(default=50, ge=1, le=TRADES_MAX_PAGE),
        before: int | None = Query(default=None, ge=1),
        _: Principal = Depends(require_auth),
    ) -> TradePage:
        """List one page of persisted trade events for ``bot_id``, newest first.

        History is unbounded, so this pages rather than returning everything:
        ``limit`` is capped server-side and ``before`` walks backward through
        stable per-bot sequence numbers.

        Args:
            bot_id: UUID of the bot.
            limit: Maximum trades to return.
            before: Return only trades with a ``seq`` below this cursor.

        Returns:
            A page of trade events and the cursor for the next page.

        Raises:
            HTTPException: If the bot does not exist.
        """
        if supervisor.get(bot_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="bot not found")
        records, next_cursor = store.read_trades(bot_id, limit=limit, before_seq=before)
        return TradePage(
            items=[TradeView.from_record(record) for record in records],
            next_cursor=next_cursor,
        )

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """Stream supervisor events to authenticated browser clients.

        Authentication uses the session cookie sent on the upgrade — no secret in
        the URL. The ``Origin`` header must pass the allowlist so a cross-site
        page cannot open a socket against a logged-in operator's cookie.

        Args:
            websocket: The pending WebSocket connection.
        """
        supervisor = websocket.app.state.supervisor
        sessions = websocket.app.state.sessions
        origin = websocket.headers.get("origin")
        host = websocket.headers.get("host")
        session = sessions.resolve(websocket.cookies.get(SESSION_COOKIE))
        if not _origin_allowed(origin, host) or session is None:
            await websocket.close(code=1008)
            return
        await websocket.accept()
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
        """Return the SPA entry point for every non-API deep link."""
        del full_path
        return FileResponse(str(index))
