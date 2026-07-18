"""In-memory login throttling to blunt password-guessing.

Failed logins are counted per key (the username and the client IP are tracked
independently); once a key crosses the failure threshold it is locked out for a
cooldown window. This is an internal-deployment policy — process-local, no
external store — sized to slow brute force without locking out a fat-fingered
operator for long. Both parameters are environment-tunable:

- ``TRADINGBOT_LOGIN_MAX_FAILURES`` (default 5)
- ``TRADINGBOT_LOGIN_LOCKOUT_SECONDS`` (default 300)
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

_DEFAULT_MAX_FAILURES = 5
_DEFAULT_LOCKOUT_SECONDS = 300


class LoginLocked(Exception):
    """Raised when a key is currently locked out.

    Attributes:
        retry_after: Whole seconds until the lock expires (>= 1).
    """

    def __init__(self, retry_after: float) -> None:
        self.retry_after = max(1, int(retry_after))
        super().__init__(f"locked out for {self.retry_after}s")


def _env_int(name: str, default: int) -> int:
    """Return a positive int from ``name`` or ``default``."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass
class _Entry:
    failures: int = 0
    locked_until: float = 0.0


@dataclass
class LoginGuard:
    """Track and throttle failed login attempts per key."""

    max_failures: int = field(default_factory=lambda: _env_int("TRADINGBOT_LOGIN_MAX_FAILURES", _DEFAULT_MAX_FAILURES))
    lockout_seconds: int = field(default_factory=lambda: _env_int("TRADINGBOT_LOGIN_LOCKOUT_SECONDS", _DEFAULT_LOCKOUT_SECONDS))
    clock: Callable[[], float] = time.monotonic
    _entries: dict[str, _Entry] = field(default_factory=dict)

    def check(self, *keys: str) -> None:
        """Raise :class:`LoginLocked` if any of ``keys`` is currently locked.

        Args:
            keys: Identifiers to check (e.g. the username and the client IP).

        Raises:
            LoginLocked: If a key's lock has not yet expired.
        """
        now = self.clock()
        for key in keys:
            entry = self._entries.get(key)
            if entry is not None and entry.locked_until > now:
                raise LoginLocked(entry.locked_until - now)

    def record_failure(self, *keys: str) -> None:
        """Count a failed attempt for each key, locking any that cross the threshold."""
        now = self.clock()
        for key in keys:
            entry = self._entries.setdefault(key, _Entry())
            # A lapsed lock resets the counter so a later burst starts fresh.
            if entry.locked_until and entry.locked_until <= now:
                entry.failures = 0
                entry.locked_until = 0.0
            entry.failures += 1
            if entry.failures >= self.max_failures:
                entry.locked_until = now + self.lockout_seconds

    def record_success(self, *keys: str) -> None:
        """Clear any recorded failures for each key after a successful login."""
        for key in keys:
            self._entries.pop(key, None)
