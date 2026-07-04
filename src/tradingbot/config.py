import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True, repr=False)
class Config:
    bybit_api_key: str
    bybit_api_secret: str
    bybit_testnet: bool
    symbol: str
    timeframe: str
    order_qty: float

    def __repr__(self) -> str:
        masked_key = "***" if self.bybit_api_key else ""
        masked_secret = "***" if self.bybit_api_secret else ""
        return (
            f"Config(bybit_api_key={masked_key!r}, bybit_api_secret={masked_secret!r}, "
            f"bybit_testnet={self.bybit_testnet!r}, symbol={self.symbol!r}, "
            f"timeframe={self.timeframe!r}, order_qty={self.order_qty!r})"
        )


def _as_bool(value: str, default: bool) -> bool:
    v = value.strip().lower()
    if v == "":
        return default
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default  # unrecognized → safe default (never silently go live on a typo)


def load_config(env: Mapping[str, str] | None = None) -> Config:
    env = os.environ if env is None else env
    order_qty_raw = (env.get("ORDER_QTY") or "").strip() or "0.001"
    try:
        order_qty = float(order_qty_raw)
    except ValueError as exc:
        raise ConfigError(f"Invalid ORDER_QTY: {order_qty_raw!r}") from exc
    return Config(
        bybit_api_key=(env.get("BYBIT_API_KEY") or "").strip(),
        bybit_api_secret=(env.get("BYBIT_API_SECRET") or "").strip(),
        bybit_testnet=_as_bool(env.get("BYBIT_TESTNET", ""), default=True),
        symbol=(env.get("SYMBOL") or "BTCUSDT").strip(),
        timeframe=(env.get("TIMEFRAME") or "5").strip(),
        order_qty=order_qty,
    )


def require_bybit_credentials(cfg: Config) -> None:
    if not cfg.bybit_api_key or not cfg.bybit_api_secret:
        raise ConfigError("Missing BYBIT_API_KEY / BYBIT_API_SECRET")
