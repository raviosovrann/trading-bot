"""File-based persistence for bot configs, trades, secrets and users."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX platforms
    _fcntl = None  # type: ignore[assignment]

from .crypto import decrypt, encrypt
from .supervisor import BotConfig

_log = logging.getLogger(__name__)

_BOT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
"""Allowed characters in a bot identifier used for filesystem paths."""

_THREAD_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[Path, Any] = {}


def _thread_lock_for(data_dir: Path) -> Any:
    """Return the process-local reentrant lock shared by a data directory."""
    key = data_dir.resolve()
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


def _validate_bot_id(bot_id: str) -> None:
    """Raise ``ValueError`` if ``bot_id`` is not a safe path identifier.

    Args:
        bot_id: Identifier to validate.

    Raises:
        ValueError: If the identifier is empty or contains characters other than
            alphanumeric characters, underscores, or hyphens.
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
        if _fcntl is None:
            raise RuntimeError(
                "BotStore requires POSIX flock support for safe multi-process access"
            )
        self._data_dir = Path(data_dir)
        self._thread_lock = _thread_lock_for(self._data_dir)
        self._lock_file = self._data_dir / ".store.lock"
        self._bots_file = self._data_dir / "bots.json"
        self._trades_dir = self._data_dir / "trades"
        self._secrets_file = self._data_dir / "secrets.json"
        self._users_file = self._data_dir / "users.json"
        self._sessions_file = self._data_dir / "sessions.json"
        self._audit_file = self._data_dir / "audit.jsonl"
        self._secure_directory(self._data_dir)
        self._harden_existing_paths()

    def save_config(self, cfg: BotConfig) -> None:
        """Persist ``cfg`` to the bot config file.

        Args:
            cfg: Bot configuration to save. Credentials are stripped before writing.
        """
        with self._transaction():
            configs = self._load_configs()
            by_id = {c.id: c for c in configs}
            by_id[cfg.id] = cfg
            self._save_configs(by_id.values())

    def load_configs(self) -> list[BotConfig]:
        """Load all persisted bot configurations.

        Returns:
            List of ``BotConfig`` records, or an empty list when the file is missing or invalid.
        """
        return self._load_configs()

    def _load_configs(self) -> list[BotConfig]:
        """Load bot configurations without acquiring the mutation lock."""
        if not self._bots_file.exists():
            return []
        text = self._bots_file.read_text(encoding="utf-8")
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
        records: list[dict[str, Any]] = []
        for cfg in configs:
            record = asdict(cfg)
            record.pop("creds", None)
            records.append(record)
        self._save_text_unlocked(self._bots_file, json.dumps(records, indent=2))

    def append_trade(self, bot_id: str, order_event: dict[str, Any]) -> None:
        """Append an order event to a bot's JSONL trade log.

        Args:
            bot_id: Bot identifier used as the log filename.
            order_event: Event data to append.

        Raises:
            ValueError: If ``bot_id`` is not a safe identifier.
        """
        _validate_bot_id(bot_id)
        with self._transaction():
            self._secure_directory(self._trades_dir)
            path = self._trades_dir / f"{bot_id}.jsonl"
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps(order_event, separators=(",", ":")) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._fsync_directory(self._trades_dir)

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
        """Load and decrypt venue credentials from ``secrets.json``.

        The file holds a single encrypted token (see :mod:`.crypto`). A missing
        or empty file yields an empty dict; a token that cannot be decrypted
        (bad JSON, wrong/absent ``TRADINGBOT_SECRETS_KEY``) is logged and treated
        as empty so a misconfiguration fails closed rather than crashing.

        Returns:
            Parsed secrets dictionary, or an empty dictionary when missing/invalid.
        """
        return self._load_secrets()

    def _load_secrets(self) -> dict[str, Any]:
        """Load secrets without acquiring the mutation lock."""
        if not self._secrets_file.exists():
            return {}
        token = self._secrets_file.read_text(encoding="utf-8").strip()
        if not token:
            return {}
        try:
            data = json.loads(decrypt(token))
        except Exception as exc:  # noqa: BLE001 - fail closed on any decrypt/parse error
            _log.warning("could not decrypt %s: %s", self._secrets_file, exc)
            return {}
        return data if isinstance(data, dict) else {}

    def save_secrets(self, venue: str, market_type: str, creds: dict[str, Any]) -> None:
        """Encrypt and persist venue credentials under ``[venue][market_type]``.

        Merges into the existing secrets so unrelated venue/market pairs are kept.
        Identifiers are normalized (``strip().lower()``) to match how the hub and
        service look secrets up. The whole blob is encrypted at rest via
        :mod:`.crypto`; secret values are never logged.

        Args:
            venue: Venue identifier, e.g. ``coinbase``.
            market_type: Market type identifier, e.g. ``spot`` or ``futures``.
            creds: Credential mapping to store for the pair.
        """
        venue = venue.strip().lower()
        market_type = market_type.strip().lower()
        with self._transaction():
            secrets = self._load_secrets()
            venue_secrets = secrets.get(venue)
            if not isinstance(venue_secrets, dict):
                venue_secrets = {}
            venue_secrets[market_type] = creds
            secrets[venue] = venue_secrets
            self._save_text_unlocked(self._secrets_file, encrypt(json.dumps(secrets)))

    def load_users(self) -> dict[str, Any]:
        """Load user/token records from ``users.json``.

        Returns:
            Parsed users dictionary, or an empty dictionary when missing or invalid.
        """
        return self._load_json(self._users_file)

    def save_users(self, data: dict[str, Any]) -> None:
        """Atomically persist user/token records to ``users.json``.

        Args:
            data: Users mapping (e.g. ``{"users": [...]}``) to write.
        """
        with self._transaction():
            self._save_json_unlocked(self._users_file, data)

    def add_user(self, record: Mapping[str, Any]) -> bool:
        """Append a new user record if the username is not already taken.

        Args:
            record: User mapping containing at least ``username``.

        Returns:
            ``True`` when the user was added; ``False`` if the username exists.
        """
        with self._transaction():
            data = self._load_json(self._users_file)
            users = data.get("users", []) if isinstance(data, dict) else []
            if not isinstance(users, list):
                users = []
            username = record.get("username")
            if any(isinstance(u, dict) and u.get("username") == username for u in users):
                return False
            users.append(dict(record))
            self._save_json_unlocked(self._users_file, {"users": users})
            return True

    def update_user(
        self,
        username: str,
        *,
        updates: Mapping[str, Any],
        expected: Mapping[str, Any] | None = None,
    ) -> bool:
        """Atomically update fields on one existing user record.

        Args:
            username: Stable username identifying the record to update.
            updates: Field values to store. The username itself cannot be changed.
            expected: Optional compare-and-set fields that must still match.

        Returns:
            ``True`` when the user matched and was updated; otherwise ``False``.
        """
        if "username" in updates:
            raise ValueError("update_user cannot change username")
        with self._transaction():
            data = self._load_json(self._users_file)
            users = data.get("users", []) if isinstance(data, dict) else []
            if not isinstance(users, list):
                return False
            user = next(
                (
                    item
                    for item in users
                    if isinstance(item, dict) and item.get("username") == username
                ),
                None,
            )
            if user is None:
                return False
            if expected is not None and any(
                user.get(key) != value for key, value in expected.items()
            ):
                return False
            user.update(updates)
            self._save_json_unlocked(self._users_file, data)
            return True

    def _load_sessions_list(self) -> list[dict[str, Any]]:
        """Return the session records list without acquiring the mutation lock."""
        data = self._load_json(self._sessions_file)
        sessions = data.get("sessions", [])
        if not isinstance(sessions, list):
            return []
        return [s for s in sessions if isinstance(s, dict)]

    def get_session(self, id_hash: str) -> dict[str, Any] | None:
        """Return the session record whose ``id_hash`` matches, or ``None``.

        Args:
            id_hash: SHA-256 hash of the raw session id.

        Returns:
            The stored session mapping, or ``None`` when no session matches.
        """
        return next(
            (s for s in self._load_sessions_list() if s.get("id_hash") == id_hash),
            None,
        )

    def add_session(self, record: Mapping[str, Any]) -> None:
        """Append a new session record.

        Args:
            record: Session mapping containing at least ``id_hash`` and ``user_id``.
        """
        with self._transaction():
            sessions = self._load_sessions_list()
            sessions.append(dict(record))
            self._save_json_unlocked(self._sessions_file, {"sessions": sessions})

    def touch_session(self, id_hash: str, last_seen: float) -> bool:
        """Update the ``last_seen`` timestamp of one session.

        Args:
            id_hash: SHA-256 hash of the raw session id.
            last_seen: New activity timestamp to store.

        Returns:
            ``True`` when a session matched and was updated; otherwise ``False``.
        """
        with self._transaction():
            sessions = self._load_sessions_list()
            for session in sessions:
                if session.get("id_hash") == id_hash:
                    session["last_seen"] = last_seen
                    self._save_json_unlocked(self._sessions_file, {"sessions": sessions})
                    return True
            return False

    def delete_session(self, id_hash: str) -> None:
        """Remove the session whose ``id_hash`` matches, if present.

        Args:
            id_hash: SHA-256 hash of the raw session id to revoke.
        """
        with self._transaction():
            sessions = self._load_sessions_list()
            kept = [s for s in sessions if s.get("id_hash") != id_hash]
            if len(kept) != len(sessions):
                self._save_json_unlocked(self._sessions_file, {"sessions": kept})

    def delete_user_sessions(self, user_id: str) -> int:
        """Remove every session belonging to ``user_id``.

        Args:
            user_id: Stable principal id whose sessions should be revoked.

        Returns:
            The number of sessions removed.
        """
        with self._transaction():
            sessions = self._load_sessions_list()
            kept = [s for s in sessions if s.get("user_id") != user_id]
            removed = len(sessions) - len(kept)
            if removed:
                self._save_json_unlocked(self._sessions_file, {"sessions": kept})
            return removed

    def append_audit(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Append one hash-chained audit record and return it.

        The chain (``seq`` + ``prev_hash`` + ``hash``) is computed under the
        store transaction so concurrent writers cannot fork or reorder it. Each
        record's ``hash`` covers the previous hash and the canonical record, so
        any edit or deletion breaks verification downstream.

        Args:
            payload: Already-redacted event fields (actor, action, target, ...).

        Returns:
            The full stored record including ``seq``, ``prev_hash`` and ``hash``.
        """
        with self._transaction():
            last = self._last_audit_record()
            seq = int(last["seq"]) + 1 if last else 1
            prev_hash = str(last["hash"]) if last else ""
            record: dict[str, Any] = {"seq": seq, "prev_hash": prev_hash, **dict(payload)}
            canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
            record["hash"] = hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()
            line = json.dumps(record, separators=(",", ":")) + "\n"
            fd = os.open(self._audit_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            self._fsync_directory(self._data_dir)
            return record

    def _load_audit_records(self) -> list[dict[str, Any]]:
        """Return all audit records in order, skipping any unparseable lines."""
        if not self._audit_file.exists():
            return []
        records: list[dict[str, Any]] = []
        with self._audit_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    _log.warning("invalid audit line in %s: %s", self._audit_file, exc)
        return records

    def _last_audit_record(self) -> dict[str, Any] | None:
        """Return the most recent audit record, or ``None`` when the log is empty."""
        records = self._load_audit_records()
        return records[-1] if records else None

    def read_audit(
        self, *, limit: int = 50, before_seq: int | None = None
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Return a page of audit records, newest first.

        Args:
            limit: Maximum records to return.
            before_seq: Return only records with ``seq`` below this cursor
                (exclusive), for paging backward through history.

        Returns:
            A ``(records, next_cursor)`` pair; ``next_cursor`` is the ``seq`` to
            pass as ``before_seq`` for the next page, or ``None`` when exhausted.
        """
        records = self._load_audit_records()
        records.sort(key=lambda r: int(r.get("seq", 0)), reverse=True)
        if before_seq is not None:
            records = [r for r in records if int(r.get("seq", 0)) < before_seq]
        page = records[: max(0, limit)]
        next_cursor = int(page[-1]["seq"]) if len(page) == limit and page else None
        return page, next_cursor

    def verify_audit_chain(self) -> bool:
        """Return whether the on-disk audit chain is intact (tamper check)."""
        prev_hash = ""
        for record in self._load_audit_records():
            stored_hash = record.get("hash")
            body = {k: v for k, v in record.items() if k != "hash"}
            canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
            expected = hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()
            if stored_hash != expected or record.get("prev_hash") != prev_hash:
                return False
            prev_hash = str(stored_hash)
        return True

    def _save_json_unlocked(self, path: Path, data: dict[str, Any]) -> None:
        """Atomically write ``data`` as JSON to ``path``.

        Args:
            path: Destination file.
            data: JSON-serializable mapping to persist.
        """
        self._save_text_unlocked(path, json.dumps(data, indent=2))

    def _save_text_unlocked(self, path: Path, text: str) -> None:
        """Atomically write ``text`` to ``path``.

        Args:
            path: Destination file.
            text: Content to persist.
        """
        tmp = self._data_dir / f"{path.stem}-{uuid.uuid4()}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(path))
            path.chmod(0o600)
            self._fsync_directory(self._data_dir)
        finally:
            tmp.unlink(missing_ok=True)

    def _load_json(self, path: Path) -> dict[str, Any]:
        """Load a JSON object from ``path``.

        Args:
            path: File to read.

        Returns:
            Parsed dictionary, or an empty dictionary when missing or invalid.
        """
        if not path.exists():
            return {}
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            _log.warning("could not parse %s: %s", path, exc)
            return {}
        return data if isinstance(data, dict) else {}

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Serialize one mutation across threads and operating-system processes."""
        fcntl_module = _fcntl
        if fcntl_module is None:  # pragma: no cover - rejected during initialization
            raise RuntimeError(
                "BotStore requires POSIX flock support for safe multi-process access"
            )
        with self._thread_lock:
            self._secure_directory(self._data_dir)
            fd = os.open(self._lock_file, os.O_RDWR | os.O_CREAT, 0o600)
            os.fchmod(fd, 0o600)
            locked = False
            try:
                fcntl_module.flock(fd, fcntl_module.LOCK_EX)
                locked = True
                yield
            finally:
                if locked:
                    fcntl_module.flock(fd, fcntl_module.LOCK_UN)
                os.close(fd)

    @staticmethod
    def _secure_directory(path: Path) -> None:
        """Create ``path`` if needed and enforce owner-only permissions."""
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)

    def _harden_existing_paths(self) -> None:
        """Restrict permissions on records created by older releases."""
        for path in (
            self._bots_file,
            self._secrets_file,
            self._users_file,
            self._sessions_file,
            self._audit_file,
        ):
            if path.is_file():
                path.chmod(0o600)
        if self._trades_dir.is_dir():
            self._trades_dir.chmod(0o700)
            for path in self._trades_dir.glob("*.jsonl"):
                if path.is_file():
                    path.chmod(0o600)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        """Persist directory metadata after creating or replacing a record."""
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
