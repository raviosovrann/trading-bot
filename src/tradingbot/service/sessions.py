"""Server-side, revocable browser sessions.

The browser holds only a random session id in a ``Secure; HttpOnly;
SameSite=Strict`` cookie. This module stores the *hash* of that id together with
the owning user, a CSRF token, and idle/absolute lifetime anchors, so a leaked
``sessions.json`` cannot be replayed and any session can be revoked immediately.

Persistence is delegated to :class:`~tradingbot.service.store.BotStore`, which
provides the transactional, permission-hardened file storage; this class owns
only the session *semantics* (id generation, hashing, expiry, CSRF).
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from .store import BotStore

# Idle timeout: a session unused for this long is expired. Absolute lifetime: a
# session is never valid past this age, even with continuous activity.
_DEFAULT_IDLE_TTL = 30 * 60
_DEFAULT_ABSOLUTE_TTL = 12 * 60 * 60


def _env_ttl(name: str, default: int) -> int:
    """Return a positive integer TTL from ``name`` or ``default``."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def hash_session_id(raw_id: str) -> str:
    """Return the SHA-256 hex digest used to store a raw session id."""
    return hashlib.sha256(raw_id.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Session:
    """A live, validated session.

    Attributes:
        user_id: Stable id of the owning principal.
        csrf_token: Token the browser must echo in ``X-CSRF-Token``.
        created_at: Absolute-lifetime anchor (epoch seconds).
        last_seen: Idle-timeout anchor (epoch seconds).
    """

    user_id: str
    csrf_token: str
    created_at: float
    last_seen: float


class SessionStore:
    """Create, resolve, and revoke browser sessions backed by a ``BotStore``."""

    def __init__(
        self,
        store: BotStore,
        *,
        idle_ttl: int | None = None,
        absolute_ttl: int | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Initialize the session store.

        Args:
            store: Persistence layer that holds the session records.
            idle_ttl: Seconds of inactivity before a session expires. Defaults to
                ``TRADINGBOT_SESSION_IDLE_TTL`` or 30 minutes.
            absolute_ttl: Maximum session age in seconds regardless of activity.
                Defaults to ``TRADINGBOT_SESSION_ABSOLUTE_TTL`` or 12 hours.
            clock: Callable returning the current epoch time (injectable for tests).
        """
        self._store = store
        self._idle_ttl = (
            idle_ttl if idle_ttl is not None
            else _env_ttl("TRADINGBOT_SESSION_IDLE_TTL", _DEFAULT_IDLE_TTL)
        )
        self._absolute_ttl = (
            absolute_ttl if absolute_ttl is not None
            else _env_ttl("TRADINGBOT_SESSION_ABSOLUTE_TTL", _DEFAULT_ABSOLUTE_TTL)
        )
        self._clock = clock

    def create(self, user_id: str) -> tuple[str, str]:
        """Create a fresh session for ``user_id``.

        Args:
            user_id: Stable id of the authenticating principal.

        Returns:
            A ``(raw_session_id, csrf_token)`` pair. Only the hash of the raw id
            is persisted; the caller places the raw id in the session cookie.
        """
        raw_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        now = self._clock()
        self._store.add_session(
            {
                "id_hash": hash_session_id(raw_id),
                "user_id": user_id,
                "csrf_token": csrf_token,
                "created_at": now,
                "last_seen": now,
            }
        )
        return raw_id, csrf_token

    def resolve(self, raw_id: str | None) -> Session | None:
        """Validate ``raw_id`` and return its live session, or ``None``.

        Expired sessions (idle or absolute) are deleted and treated as absent.
        A resolved session's ``last_seen`` is refreshed to now.

        Args:
            raw_id: Raw session id from the cookie, or ``None`` when absent.

        Returns:
            The live :class:`Session`, or ``None`` if missing or expired.
        """
        if not raw_id:
            return None
        id_hash = hash_session_id(raw_id)
        record = self._store.get_session(id_hash)
        if record is None:
            return None
        created_at = float(record.get("created_at", 0.0))
        last_seen = float(record.get("last_seen", 0.0))
        now = self._clock()
        if now - created_at > self._absolute_ttl or now - last_seen > self._idle_ttl:
            self._store.delete_session(id_hash)
            return None
        self._store.touch_session(id_hash, now)
        return Session(
            user_id=str(record.get("user_id", "")),
            csrf_token=str(record.get("csrf_token", "")),
            created_at=created_at,
            last_seen=now,
        )

    def revoke(self, raw_id: str | None) -> None:
        """Revoke the session identified by ``raw_id`` if present."""
        if raw_id:
            self._store.delete_session(hash_session_id(raw_id))

    def revoke_user(self, user_id: str) -> int:
        """Revoke every session belonging to ``user_id``; return the count removed."""
        return self._store.delete_user_sessions(user_id)
