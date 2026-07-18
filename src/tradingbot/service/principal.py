"""The authenticated identity attached to a request.

``require_auth`` resolves a session cookie or a direct-API bearer token to a
``Principal`` so downstream code — and, later, the audit trail — can attribute
every sensitive action to a stable user or service account instead of an opaque
token.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    """A resolved, authenticated identity.

    Attributes:
        id: Stable identifier for the user or service account.
        username: Human-readable name (equals ``id`` for legacy records).
        roles: Assigned roles, e.g. ``("admin",)`` or ``("operator",)``.
        kind: ``"user"`` for interactive operators, ``"service"`` for
            programmatic API-token callers.
    """

    id: str
    username: str
    roles: tuple[str, ...]
    kind: str

    def has_role(self, role: str) -> bool:
        """Return whether this principal holds ``role``."""
        return role in self.roles
