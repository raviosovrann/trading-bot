"""Default service entrypoint wiring for the Trading Console.

This module provides a zero-argument ``create_service_app`` factory suitable for
running with uvicorn's ``--factory`` flag. It wires a file-based store and an
empty supervisor. The ``hub_factory`` must be supplied before bots can be
started; by default it raises a clear error pointing the operator to configure
it for their venue.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .api import create_app
from .events import EventBus
from .health import validate_startup
from .hub_factory import HubFactory
from .exposure import ExposureTracker
from .store import BotStore
from .supervisor import BotSupervisor

_log = logging.getLogger(__name__)


def create_service_app() -> Any:
    """Create the FastAPI app with default file-based persistence.

    Bots draw market data from a shared ``HubFactory`` — one hub per
    ``(venue, market_type, timeframe)`` and one rate limiter per account — so
    the account rate limit is respected regardless of bot count.

    Returns:
        Configured FastAPI application.
    """
    data_dir = Path(os.environ.get("TRADINGBOT_DATA_DIR", "data"))
    store = BotStore(data_dir)
    # Fail closed in production; otherwise surface problems prominently so a
    # developer sees them instead of hitting a silent failure later.
    for problem in validate_startup(store):
        _log.error("startup check failed: %s", problem)
    supervisor = BotSupervisor(
        hub_factory=HubFactory(store),
        event_bus=EventBus(),
        exposure=ExposureTracker(),
        store=store,
    )
    # Serve the built SPA from ui/dist (repo root) when present; TRADINGBOT_UI_DIST
    # overrides the location.
    default_dist = Path(__file__).resolve().parents[3] / "ui" / "dist"
    spa_dir = Path(os.environ.get("TRADINGBOT_UI_DIST", str(default_dist)))
    return create_app(store=store, supervisor=supervisor, spa_dir=spa_dir)
