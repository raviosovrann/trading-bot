"""Liveness/readiness checks and startup validation.

Liveness answers "is the process alive?"; readiness answers "can this process
safely serve traffic *right now*?". They are deliberately different: a process
can be alive while its data directory is unwritable or its secrets key is wrong,
and serving in that state silently loses trades and credentials.

Startup validation applies the same checks once at boot so a misconfigured
deployment fails loudly (and, in production, fails closed) instead of coming up
half-broken.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .crypto import decrypt
from .store import BotStore


class StartupError(RuntimeError):
    """Raised when the service is too misconfigured to start safely."""


@dataclass(frozen=True)
class Check:
    """One readiness probe result.

    Attributes:
        ok: Whether the dependency is usable.
        detail: Short human-readable explanation (never contains secrets).
    """

    ok: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        """Return the check as a JSON-serializable mapping."""
        return {"ok": self.ok, "detail": self.detail}


def is_production() -> bool:
    """Return whether the service is configured to run in production mode."""
    return os.environ.get("TRADINGBOT_ENV", "").strip().lower() == "production"


def check_storage(store: BotStore) -> Check:
    """Verify the data directory is readable and writable.

    Writes and removes a uniquely named probe file, which catches a read-only
    mount, a full disk, or wrong ownership — none of which a mere ``exists()``
    check would notice.
    """
    data_dir = Path(store.data_dir)
    probe = data_dir / f".readiness-{uuid.uuid4().hex}"
    try:
        if not data_dir.is_dir():
            return Check(False, "data directory does not exist")
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check(False, f"data directory is not writable: {exc.strerror or 'error'}")
    return Check(True, "data directory is readable and writable")


def check_secrets_key(store: BotStore) -> Check:
    """Verify the secrets key is present and still decrypts stored secrets.

    A rotated or wrong ``TRADINGBOT_SECRETS_KEY`` would otherwise surface only
    when a bot tries to start and finds no credentials ("key continuity").
    """
    if not os.environ.get("TRADINGBOT_SECRETS_KEY", "").strip():
        return Check(False, "TRADINGBOT_SECRETS_KEY is not set")
    secrets_file = Path(store.data_dir) / "secrets.json"
    if not secrets_file.is_file():
        return Check(True, "secrets key present (no secrets stored yet)")
    token = secrets_file.read_text(encoding="utf-8").strip()
    if not token:
        return Check(True, "secrets key present (secrets file empty)")
    try:
        decrypt(token)
    except Exception:  # noqa: BLE001 - any decrypt failure means the key is wrong
        return Check(False, "secrets key cannot decrypt the stored secrets")
    return Check(True, "secrets key decrypts stored secrets")


def readiness(store: BotStore) -> tuple[bool, dict[str, Any]]:
    """Run every readiness probe.

    Args:
        store: Persistence layer to probe.

    Returns:
        ``(ready, checks)`` where ``checks`` maps a probe name to its result.
    """
    checks = {"storage": check_storage(store), "secrets_key": check_secrets_key(store)}
    ready = all(check.ok for check in checks.values())
    return ready, {name: check.as_dict() for name, check in checks.items()}


def validate_startup(store: BotStore) -> list[str]:
    """Validate configuration at boot.

    Returns a list of human-readable problems. In production mode any problem is
    fatal (:class:`StartupError`) so the service fails closed rather than serving
    while unable to persist trades or read credentials.

    Args:
        store: Persistence layer to validate.

    Returns:
        Problems found (empty when healthy) — for logging in non-production.

    Raises:
        StartupError: In production when any required check fails.
    """
    _ready, checks = readiness(store)
    problems = [f"{name}: {result['detail']}" for name, result in checks.items() if not result["ok"]]

    if is_production():
        # A production deployment must sit behind a TLS-terminating proxy whose
        # forwarded host/proto we trust; serving without it would issue session
        # cookies over plaintext.
        if not os.environ.get("TRADINGBOT_ALLOWED_ORIGINS", "").strip():
            problems.append(
                "TRADINGBOT_ALLOWED_ORIGINS: must list the operator origin(s) in production"
            )
        if os.environ.get("TRADINGBOT_COOKIE_SECURE", "").strip().lower() in ("0", "false", "no", "off"):
            problems.append("TRADINGBOT_COOKIE_SECURE: must not be disabled in production")
        if problems:
            raise StartupError(
                "refusing to start in production with an unsafe configuration: "
                + "; ".join(problems)
            )
    return problems
