"""Append-only audit trail for sensitive operator actions.

Records who did what, when, and with what outcome for safety-critical events
(authentication, credential changes, bot lifecycle, live-mode toggles, risk
limits). Records are hash-chained by :class:`~tradingbot.service.store.BotStore`
so tampering is detectable, and every payload passes through a redaction step so
secrets, passwords, session ids, and tokens never enter the log.

Retention: the log is a single ``audit.jsonl`` under the data directory
(``0600``, admin-read-only via ``GET /api/audit``). Operators rotate/retain it
with their normal backup policy; see the runbook.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from .principal import Principal
from .store import BotStore

# Keys whose values are always masked, wherever they appear in before/after.
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "new_password",
        "token",
        "token_hash",
        "api_key",
        "api_secret",
        "api_password",
        "secret",
        "secrets",
        "creds",
        "credentials",
        "csrf_token",
        "session",
        "id_hash",
    }
)
_MASK = "***"


def redact(value: Any) -> Any:
    """Return a copy of ``value`` with sensitive fields masked.

    Masks by key name at any depth. A key that merely *contains* a sensitive
    token (e.g. ``api_key_id``) is also masked, so new credential-shaped fields
    fail closed rather than leaking.

    Args:
        value: Any JSON-serializable structure, or ``None``.

    Returns:
        The redacted structure (``None`` stays ``None``).
    """
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(sensitive in lowered for sensitive in _SENSITIVE_KEYS):
                redacted[key] = _MASK
            else:
                redacted[key] = redact(item)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class AuditLog:
    """Write redacted, hash-chained audit records through a ``BotStore``."""

    def __init__(self, store: BotStore, *, clock: Callable[[], float] = time.time) -> None:
        """Initialize the audit log.

        Args:
            store: Persistence layer that appends and chains the records.
            clock: Callable returning the current epoch time (injectable for tests).
        """
        self._store = store
        self._clock = clock

    def record(
        self,
        *,
        actor: Principal | None,
        action: str,
        target: str,
        request_id: str,
        outcome: str,
        before: Any = None,
        after: Any = None,
    ) -> dict[str, Any]:
        """Append one audit event.

        Args:
            actor: The authenticated principal, or ``None`` for pre-auth events
                (e.g. a failed login).
            action: Dotted action name, e.g. ``"bot.start"`` or ``"login"``.
            target: The affected resource, e.g. ``"bot:<id>"`` or ``"user:<name>"``.
            request_id: Correlation id from the request-id middleware.
            outcome: ``"success"``, ``"failure"``, or ``"denied"``.
            before: Prior state for a mutation (redacted before storage).
            after: New state for a mutation (redacted before storage).

        Returns:
            The stored record, including its chain fields.
        """
        payload = {
            "ts": self._clock(),
            "actor_id": actor.id if actor else "anonymous",
            "actor_name": actor.username if actor else "anonymous",
            "actor_kind": actor.kind if actor else "anonymous",
            "action": action,
            "target": target,
            "request_id": request_id,
            "outcome": outcome,
            "before": redact(before),
            "after": redact(after),
        }
        return self._store.append_audit(payload)
