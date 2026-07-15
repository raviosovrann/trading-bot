import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True, repr=False)
class Config:
    exchange: str
    api_key: str
    api_secret: str
    api_password: str
    symbol: str
    timeframe: str
    order_qty: float
    stream: bool = False
    live: bool = False
    strategy: str = "example"

    def __repr__(self) -> str:
        def mask(v: str) -> str:
            return "***" if v else ""
        return (
            f"Config(exchange={self.exchange!r}, "
            f"api_key={mask(self.api_key)!r}, "
            f"api_secret={mask(self.api_secret)!r}, "
            f"api_password={mask(self.api_password)!r}, "
            f"symbol={self.symbol!r}, timeframe={self.timeframe!r}, "
            f"order_qty={self.order_qty!r}, strategy={self.strategy!r}, "
            f"stream={self.stream!r}, live={self.live!r})"
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
        exchange=(env.get("EXCHANGE") or "coinbase").strip().lower(),
        api_key=(env.get("API_KEY") or "").strip(),
        api_secret=(env.get("API_SECRET") or "").strip(),
        api_password=(env.get("API_PASSWORD") or "").strip(),
        symbol=(env.get("SYMBOL") or "BTC/USD").strip(),
        timeframe=(env.get("TIMEFRAME") or "5m").strip(),
        order_qty=order_qty,
        strategy=(env.get("STRATEGY") or "example").strip() or "example",
        stream=_as_bool(env.get("STREAM", ""), default=False),
        live=_as_bool(env.get("LIVE", ""), default=False),
    )


def require_credentials(cfg: Config) -> None:
    """Ensure the exchange has the credentials ccxt needs.

    Requires an API key and secret. Some exchanges (e.g. Coinbase, OKX, KuCoin)
    also need a passphrase via API_PASSWORD, but that is exchange-specific and
    not enforced here.
    """
    if not cfg.api_key or not cfg.api_secret:
        raise ConfigError("Missing API_KEY / API_SECRET")
