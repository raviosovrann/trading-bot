"""``tradingbot`` admin CLI: first-run bootstrap and user management.

Replaces hand-editing ``users.json``. Passwords are read interactively with
hidden confirmation and are never accepted as command-line arguments (which
would leak into shell history and process listings). Every change goes through
the transactional :class:`~tradingbot.service.store.BotStore`, invalidates
affected sessions, and is written to the audit trail.

Usage::

    tradingbot bootstrap --username admin
    tradingbot user add --username op [--admin]
    tradingbot user list
    tradingbot user disable --username op
    tradingbot user reset-password --username op
    tradingbot user revoke-sessions --username op

The data directory is ``TRADINGBOT_DATA_DIR`` (default ``data``).
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import uuid
from collections.abc import Callable, Sequence

from .service.audit import AuditLog
from .service.auth import WeakPasswordError, check_password_policy, hash_password
from .service.principal import Principal
from .service.sessions import SessionStore
from .service.store import BotStore

PasswordReader = Callable[[], str]


def _user_key(user: dict[str, object]) -> str:
    """Return the session ``user_id`` key for ``user`` (id, or username fallback)."""
    return str(user.get("id") or user.get("username"))


def _default_password_reader() -> str:
    """Read a password twice from the TTY, re-prompting until the two match."""
    while True:
        password = getpass.getpass("New password: ")
        if password == getpass.getpass("Confirm password: "):
            return password
        print("passwords do not match; try again", file=sys.stderr)


def _cli_principal() -> Principal:
    """Return a principal identifying the local admin running the CLI."""
    return Principal(
        id="cli", username=f"cli:{os.environ.get('USER', 'unknown')}",
        roles=("admin",), kind="service",
    )


class AdminError(Exception):
    """A user-facing CLI error (printed without a traceback)."""


def _find_user(store: BotStore, username: str) -> dict[str, object]:
    """Return the stored record for ``username`` or raise :class:`AdminError`."""
    for user in store.load_users().get("users", []):
        if isinstance(user, dict) and user.get("username") == username:
            return user
    raise AdminError(f"no such user: {username!r}")


def _collect_password(read_password: PasswordReader) -> str:
    """Read and policy-check a new password.

    Raises:
        AdminError: If the password fails the policy.
    """
    password = read_password()
    try:
        check_password_policy(password)
    except WeakPasswordError as exc:
        raise AdminError(str(exc)) from exc
    return password


def cmd_bootstrap(
    store: BotStore, sessions: SessionStore, audit: AuditLog,
    args: argparse.Namespace, read_password: PasswordReader,
) -> int:
    """Create the first administrator; refuse once any user exists."""
    del sessions
    if store.load_users().get("users"):
        raise AdminError("already initialized: users already exist (bootstrap is one-time)")
    password = _collect_password(read_password)
    record = {
        "id": str(uuid.uuid4()), "username": args.username,
        "password_hash": hash_password(password), "roles": ["admin"], "disabled": False,
    }
    if not store.add_user(record):
        raise AdminError(f"user already exists: {args.username!r}")
    audit.record(actor=_cli_principal(), action="user.bootstrap",
                 target=f"user:{args.username}", request_id="cli", outcome="success")
    print(f"created administrator {args.username!r}")
    return 0


def cmd_user_add(
    store: BotStore, sessions: SessionStore, audit: AuditLog,
    args: argparse.Namespace, read_password: PasswordReader,
) -> int:
    """Add a new operator (or admin with ``--admin``)."""
    del sessions
    password = _collect_password(read_password)
    roles = ["admin"] if args.admin else ["operator"]
    record = {
        "id": str(uuid.uuid4()), "username": args.username,
        "password_hash": hash_password(password), "roles": roles, "disabled": False,
    }
    if not store.add_user(record):
        raise AdminError(f"user already exists: {args.username!r}")
    audit.record(actor=_cli_principal(), action="user.add",
                 target=f"user:{args.username}", request_id="cli", outcome="success",
                 after={"roles": roles})
    print(f"added user {args.username!r} ({', '.join(roles)})")
    return 0


def cmd_user_list(
    store: BotStore, sessions: SessionStore, audit: AuditLog,
    args: argparse.Namespace, read_password: PasswordReader,
) -> int:
    """List users with their roles and status."""
    del sessions, audit, args, read_password
    users = store.load_users().get("users", [])
    if not users:
        print("(no users)")
        return 0
    for user in users:
        if not isinstance(user, dict):
            continue
        roles = ",".join(str(r) for r in user.get("roles", ["operator"]))
        state = "disabled" if user.get("disabled") else "active"
        has_token = "token" if user.get("token_hash") else "-"
        print(f"{user.get('username'):<20} {roles:<16} {state:<10} {has_token}")
    return 0


def cmd_user_disable(
    store: BotStore, sessions: SessionStore, audit: AuditLog,
    args: argparse.Namespace, read_password: PasswordReader,
) -> int:
    """Disable a user and revoke their active sessions."""
    del read_password
    user = _find_user(store, args.username)
    store.update_user(args.username, updates={"disabled": True})
    revoked = sessions.revoke_user(_user_key(user))
    audit.record(actor=_cli_principal(), action="user.disable",
                 target=f"user:{args.username}", request_id="cli", outcome="success",
                 after={"sessions_revoked": revoked})
    print(f"disabled {args.username!r}; revoked {revoked} session(s)")
    return 0


def cmd_user_reset_password(
    store: BotStore, sessions: SessionStore, audit: AuditLog,
    args: argparse.Namespace, read_password: PasswordReader,
) -> int:
    """Set a new password for a user and revoke their active sessions."""
    user = _find_user(store, args.username)
    password = _collect_password(read_password)
    store.update_user(args.username, updates={"password_hash": hash_password(password)})
    revoked = sessions.revoke_user(_user_key(user))
    audit.record(actor=_cli_principal(), action="user.reset_password",
                 target=f"user:{args.username}", request_id="cli", outcome="success",
                 after={"sessions_revoked": revoked})
    print(f"reset password for {args.username!r}; revoked {revoked} session(s)")
    return 0


def cmd_user_revoke_sessions(
    store: BotStore, sessions: SessionStore, audit: AuditLog,
    args: argparse.Namespace, read_password: PasswordReader,
) -> int:
    """Revoke all active sessions for a user."""
    del read_password
    user = _find_user(store, args.username)
    revoked = sessions.revoke_user(_user_key(user))
    audit.record(actor=_cli_principal(), action="user.revoke_sessions",
                 target=f"user:{args.username}", request_id="cli", outcome="success",
                 after={"sessions_revoked": revoked})
    print(f"revoked {revoked} session(s) for {args.username!r}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Build the ``tradingbot`` argument parser."""
    parser = argparse.ArgumentParser(prog="tradingbot", description="Trading console admin CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    bootstrap = sub.add_parser("bootstrap", help="create the first administrator (one-time)")
    bootstrap.add_argument("--username", required=True)
    bootstrap.set_defaults(func=cmd_bootstrap)

    user = sub.add_parser("user", help="manage users")
    usub = user.add_subparsers(dest="subcommand", required=True)

    add = usub.add_parser("add", help="add a user")
    add.add_argument("--username", required=True)
    add.add_argument("--admin", action="store_true", help="grant the admin role")
    add.set_defaults(func=cmd_user_add)

    listp = usub.add_parser("list", help="list users")
    listp.set_defaults(func=cmd_user_list)

    disable = usub.add_parser("disable", help="disable a user and revoke sessions")
    disable.add_argument("--username", required=True)
    disable.set_defaults(func=cmd_user_disable)

    reset = usub.add_parser("reset-password", help="set a new password and revoke sessions")
    reset.add_argument("--username", required=True)
    reset.set_defaults(func=cmd_user_reset_password)

    revoke = usub.add_parser("revoke-sessions", help="revoke a user's active sessions")
    revoke.add_argument("--username", required=True)
    revoke.set_defaults(func=cmd_user_revoke_sessions)

    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    read_password: PasswordReader = _default_password_reader,
    data_dir: str | None = None,
) -> int:
    """CLI entry point.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).
        read_password: Injectable password reader (for tests).
        data_dir: Override for the data directory (defaults to the env/``data``).

    Returns:
        Process exit code (0 on success, 2 on a user-facing error).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    resolved_dir = data_dir or os.environ.get("TRADINGBOT_DATA_DIR", "data")
    store = BotStore(resolved_dir)
    sessions = SessionStore(store)
    audit = AuditLog(store)
    try:
        return int(args.func(store, sessions, audit, args, read_password))
    except AdminError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
