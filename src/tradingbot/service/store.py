"""File-based persistence for bot configs, trades, secrets and users."""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .supervisor import BotConfig

_log = logging.getLogger(__name__)

_BOT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
"""Allowed characters in a bot identifier used for filesystem paths."""


def _validate_bot_id(bot_id: str) -> None:
    """Raise ``ValueError`` if ``bot_id`` is not a safe path identifier.

    Args:
        bot_id: Identifier to validate.

    Raises:
        ValueError: If the identifier is not a non-empty alphanumeric string.
    """
    if not isinstance(bot_id, str) or not _BOT_ID_RE.match(bot_id):
        raise ValueError(f"invalid bot id: {bot_id!r}")


class BotStore:
    """File-based persistence for bot configs, trade history, secrets and users."""

    def __init__(self, data_dir: str | Path) -> None:
        """Initialize paths under ``data_dir``.

        Args:
            data_dir: Directory where JSON files and trade logs are stored.
        """
        self._data_dir = Path(data_dir)
        self._bots_file = self._data_dir / "bots.json"
        self._trades_dir = self._data_dir / "trades"
        self._secrets_file = self._data_dir / "secrets.json"
        self._users_file = self._data_dir / "users.json"

    def save_config(self, cfg: BotConfig) -> None:
        """Persist ``cfg`` to the bot config file.

        Args:
            cfg: Bot configuration to save. Credentials are stripped before writing.
        """
        configs = self.load_configs()
        by_id = {c.id: c for c in configs}
        by_id[cfg.id] = cfg
        self._save_configs(by_id.values())

    def load_configs(self) -> list[BotConfig]:
        """Load all persisted bot configurations.

        Returns:
            List of ``BotConfig`` records, or an empty list when the file is missing or invalid.
        """
        if not self._bots_file.exists():
            return []
        text = self._bots_file.read_text()
        if not text.strip():
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            _log.warning("could not parse %s: %s", self._bots_file, exc)
            return []
        if not isinstance(data, list):
            return []
        return [BotConfig(**item) for item in data]

    def _save_configs(self, configs: Iterable[BotConfig]) -> None:
        """Atomically write configs to disk with credentials removed.

        Args:
            configs: Bot configurations to persist.
        """
        self._data_dir.mkdir(parents=True, exist_ok=True)
        records: list[dict[str, Any]] = []
        for cfg in configs:
            record = asdict(cfg)
            record.pop("creds", None)
            records.append(record)
        tmp = self._data_dir / f"bots-{uuid.uuid4()}.json.tmp"
        tmp.write_text(json.dumps(records, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(self._bots_file))

    def append_trade(self, bot_id: str, order_event: dict[str, Any]) -> None:
        """Append an order event to a bot's JSONL trade log.

        Args:
            bot_id: Bot identifier used as the log filename.
            order_event: Event data to append.

        Raises:
            ValueError: If ``bot_id`` is not a safe identifier.
        """
        _validate_bot_id(bot_id)
        self._trades_dir.mkdir(parents=True, exist_ok=True)
        path = self._trades_dir / f"{bot_id}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(order_event, separators=(",", ":")) + "\n")

    def read_trades(self, bot_id: str) -> list[dict[str, Any]]:
        """Read all trade events persisted for ``bot_id``.

        Args:
            bot_id: Bot identifier used as the log filename.

        Returns:
            List of parsed trade events. Invalid lines are logged and skipped.

        Raises:
            ValueError: If ``bot_id`` is not a safe identifier.
        """
        _validate_bot_id(bot_id)
        path = self._trades_dir / f"{bot_id}.jsonl"
        if not path.exists():
            return []
        trades: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    trades.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    _log.warning("invalid trade line in %s: %s", path, exc)
        return trades

    def load_secrets(self) -> dict[str, Any]:
        """Load venue credentials from ``secrets.json``.

        Returns:
            Parsed secrets dictionary, or an empty dictionary when missing or invalid.
        """
        return self._load_json(self._secrets_file)

    def load_users(self) -> dict[str, Any]:
        """Load user/token records from ``users.json``.

        Returns:
            Parsed users dictionary, or an empty dictionary when missing or invalid.
        """
        return self._load_json(self._users_file)

    def _load_json(self, path: Path) -> dict[str, Any]:
        """Load a JSON object from ``path``.

        Args:
            path: File to read.

        Returns:
            Parsed dictionary, or an empty dictionary when missing or invalid.
        """
        if not path.exists():
            return {}
        text = path.read_text()
        if not text.strip():
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            _log.warning("could not parse %s: %s", path, exc)
            return {}
        return data if isinstance(data, dict) else {}
