import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    bybit_api_key: str
    bybit_api_secret: str
    bybit_testnet: bool
    symbol: str
    timeframe: str
    order_qty: float


def _as_bool(value: str, default: bool) -> bool:
    if value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    return Config(
        bybit_api_key=(env.get("BYBIT_API_KEY") or "").strip(),
        bybit_api_secret=(env.get("BYBIT_API_SECRET") or "").strip(),
        bybit_testnet=_as_bool(env.get("BYBIT_TESTNET", ""), default=True),
        symbol=env.get("SYMBOL", "BTCUSDT"),
        timeframe=env.get("TIMEFRAME", "5"),
        order_qty=float(env.get("ORDER_QTY", "0.001")),
    )


def require_bybit_credentials(cfg: Config) -> None:
    if not cfg.bybit_api_key or not cfg.bybit_api_secret:
        raise ConfigError("Missing BYBIT_API_KEY / BYBIT_API_SECRET")
