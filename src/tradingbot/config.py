import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    webhook_token: str
    venue: str
    allowed_ips: tuple[str, ...]


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    token = (env.get("WEBHOOK_TOKEN") or "").strip()
    if not token:
        raise ConfigError("Missing required env var: WEBHOOK_TOKEN")
    allowed = tuple(
        ip.strip() for ip in env.get("ALLOWED_IPS", "").split(",") if ip.strip()
    )
    return Config(
        webhook_token=token,
        venue=env.get("VENUE", "bybit_testnet"),
        allowed_ips=allowed,
    )
