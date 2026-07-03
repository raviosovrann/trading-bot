import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    webhook_token: str
    venue: str
    allowed_ips: tuple[str, ...]


def load_config(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ) if env is None else env
    if not env.get("WEBHOOK_TOKEN"):
        raise ConfigError("Missing required env var: WEBHOOK_TOKEN")
    allowed = tuple(
        ip.strip() for ip in env.get("ALLOWED_IPS", "").split(",") if ip.strip()
    )
    return Config(
        webhook_token=env["WEBHOOK_TOKEN"],
        venue=env.get("VENUE", "bybit_testnet"),
        allowed_ips=allowed,
    )
