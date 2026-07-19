"""Turn venue exceptions into readable, secret-free HTTP responses.

A venue failure used to surface as a bare ``500``, with the real explanation
only in the server log — an operator saw "Internal Server Error" and could not
tell whether their key was wrong, the exchange was down, or the service was
broken (#175, and #170 before it).

Two jobs here, and the second is the load-bearing one:

* **Classify.** Map the failure onto a status that says *whose* problem it is:
  ``400`` for something the operator can change, ``429`` for rate limiting,
  ``502`` for the exchange being unavailable. Anything that is not a venue
  exception keeps its ``500``, so a genuine bug in this codebase is never
  disguised as an exchange problem.

* **Redact.** ccxt puts the full request URL in its error messages, and for a
  signed call that URL carries the API key and signature. Surfacing the raw
  message to an HTTP client would leak credentials into a browser, a proxy log
  and any error tracker in between.
"""

from __future__ import annotations

import re
from typing import Any

MAX_DETAIL_CHARS = 500
"""Cap on a surfaced message, so a large upstream body is not echoed wholesale."""

_SENSITIVE_KEYS = (
    "signature",
    "apikey",
    "api_key",
    "api-key",
    "secret",
    "api_secret",
    "password",
    "passphrase",
    "token",
    "access_token",
    "accesstoken",
    "key",
)
"""Parameter names whose values must never be surfaced."""

_QUERY_IN_URL = re.compile(r"(https?://[^\s?]+)\?\S*")
"""A URL with a query string. Signed ccxt requests put credentials there."""

_SENSITIVE_PARAM = re.compile(
    r"\b(" + "|".join(re.escape(key) for key in _SENSITIVE_KEYS) + r")\s*[=:]\s*[^\s,;&)\]}\"']+",
    re.IGNORECASE,
)
"""A ``key=value`` pair carrying a credential, anywhere in free text."""


def redact(message: str) -> str:
    """Strip credential material out of a venue error message.

    Query strings are dropped whole rather than filtered key by key: a signed
    request URL is credential-bearing by construction, and the parameters vary
    per exchange, so an allowlist would eventually miss one.

    Args:
        message: Raw exception text.

    Returns:
        The message with credentials removed and length capped.
    """
    safe = _QUERY_IN_URL.sub(r"\1?<redacted>", message)
    safe = _SENSITIVE_PARAM.sub(lambda match: f"{match.group(1)}=<redacted>", safe)
    if len(safe) > MAX_DETAIL_CHARS:
        safe = safe[:MAX_DETAIL_CHARS].rstrip() + "… (truncated)"
    return safe


def _ccxt_errors() -> Any:
    """Return the ccxt module, or ``None`` when it is not installed.

    Returns:
        The ccxt module, or ``None``.
    """
    try:
        import ccxt
    except Exception:  # pragma: no cover - ccxt is a pinned dependency
        return None
    return ccxt


def classify_venue_error(exc: BaseException) -> tuple[int, str] | None:
    """Map a venue exception onto an HTTP status and a safe detail.

    Args:
        exc: Exception raised while talking to a venue.

    Returns:
        ``(status, detail)`` when this is a venue failure, or ``None`` when it
        is not — in which case the caller should keep its ``500``, because the
        fault is ours rather than the exchange's.
    """
    ccxt = _ccxt_errors()
    if ccxt is None or not isinstance(exc, ccxt.BaseError):
        return None

    # Order matters: several of these subclass one another. RateLimitExceeded
    # is a NetworkError, and PermissionDenied is an AuthenticationError, so the
    # most specific case has to be tested first.
    if isinstance(exc, ccxt.RateLimitExceeded):
        status = 429
    elif isinstance(exc, ccxt.AuthenticationError):
        status = 400
    elif isinstance(exc, (ccxt.BadRequest, ccxt.NotSupported, ccxt.ArgumentsRequired)):
        status = 400
    else:
        # Everything else reaching us from ccxt is the exchange's side of the
        # conversation: unavailable, restricted, timed out, rejected.
        status = 502

    detail = redact(str(exc)).strip()
    name = type(exc).__name__
    return status, f"{name}: {detail}" if detail else name
