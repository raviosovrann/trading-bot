import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


VENUES = ("alpaca", "coinbase", "fake")


@dataclass(frozen=True, repr=False)
class Config:
    venue: str
    alpaca_api_key: str
    alpaca_api_secret: str
    alpaca_paper: bool
    coinbase_api_key: str
    coinbase_api_secret: str
    coinbase_sandbox: bool
    symbol: str
    timeframe: str
    order_qty: float

    def __repr__(self) -> str:
        def mask(v: str) -> str:
            return "***" if v else ""
        return (
            f"Config(venue={self.venue!r}, "
            f"alpaca_api_key={mask(self.alpaca_api_key)!r}, "
            f"alpaca_api_secret={mask(self.alpaca_api_secret)!r}, "
            f"alpaca_paper={self.alpaca_paper!r}, "
            f"coinbase_api_key={mask(self.coinbase_api_key)!r}, "
            f"coinbase_api_secret={mask(self.coinbase_api_secret)!r}, "
            f"coinbase_sandbox={self.coinbase_sandbox!r}, "
            f"symbol={self.symbol!r}, timeframe={self.timeframe!r}, "
            f"order_qty={self.order_qty!r})"
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

    venue = (env.get("VENUE") or "alpaca").strip().lower()
    if venue not in VENUES:
        raise ConfigError(f"Invalid VENUE: {venue!r} (expected one of {VENUES})")

    order_qty_raw = (env.get("ORDER_QTY") or "").strip() or "0.001"
    try:
        order_qty = float(order_qty_raw)
    except ValueError as exc:
        raise ConfigError(f"Invalid ORDER_QTY: {order_qty_raw!r}") from exc

    return Config(
        venue=venue,
        alpaca_api_key=(env.get("ALPACA_API_KEY") or "").strip(),
        alpaca_api_secret=(env.get("ALPACA_API_SECRET") or "").strip(),
        alpaca_paper=_as_bool(env.get("ALPACA_PAPER", ""), default=True),
        coinbase_api_key=(env.get("COINBASE_API_KEY") or "").strip(),
        coinbase_api_secret=(env.get("COINBASE_API_SECRET") or "").strip(),
        coinbase_sandbox=_as_bool(env.get("COINBASE_SANDBOX", ""), default=True),
        symbol=(env.get("SYMBOL") or "BTC/USD").strip(),
        timeframe=(env.get("TIMEFRAME") or "5Min").strip(),
        order_qty=order_qty,
    )


def require_credentials(cfg: Config) -> None:
    """Ensure the selected venue has the credentials it needs.

    The ``fake`` venue needs none. Real venues fail fast when key/secret are empty.
    """
    if cfg.venue == "alpaca":
        if not cfg.alpaca_api_key or not cfg.alpaca_api_secret:
            raise ConfigError("Missing ALPACA_API_KEY / ALPACA_API_SECRET")
    elif cfg.venue == "coinbase":
        if not cfg.coinbase_api_key or not cfg.coinbase_api_secret:
            raise ConfigError("Missing COINBASE_API_KEY / COINBASE_API_SECRET")
