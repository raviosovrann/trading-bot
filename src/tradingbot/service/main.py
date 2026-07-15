"""Default service entrypoint wiring for the Trading Console.

This module provides a zero-argument ``create_service_app`` factory suitable for
running with uvicorn's ``--factory`` flag. It wires a file-based store and an
empty supervisor. The ``hub_factory`` must be supplied before bots can be
started; by default it raises a clear error pointing the operator to configure
it for their venue.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .api import create_app
from .events import EventBus
from .risk import GlobalExposure
from .store import BotStore
from .supervisor import BotConfig, BotSupervisor


def _default_hub_factory(_cfg: BotConfig) -> Any:
    """Placeholder hub factory; real deployments should configure one per venue."""
    raise NotImplementedError(
        "No hub_factory configured. Set TRADINGBOT_HUB_FACTORY or replace "
        "tradingbot.service.main._default_hub_factory before starting bots."
    )


def create_service_app() -> Any:
    """Create the FastAPI app with default file-based persistence.

    Returns:
        Configured FastAPI application.
    """
    data_dir = Path(os.environ.get("TRADINGBOT_DATA_DIR", "data"))
    store = BotStore(data_dir)
    supervisor = BotSupervisor(
        hub_factory=_default_hub_factory,
        event_bus=EventBus(),
        global_exposure=GlobalExposure(),
    )
    return create_app(store=store, supervisor=supervisor)
