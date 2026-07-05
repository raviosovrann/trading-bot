from __future__ import annotations

import os
from collections.abc import Sequence

from .config import Config, load_config, require_credentials
from .datafeed import build_feed
from .router import SignalRouter
from .runtime import BotRuntime
from .strategy import SMACrossoverStrategy
from .venues.alpaca import AlpacaVenue
from .venues.coinbase import CoinbaseVenue
from .venues.fake import FakeVenue


def _build_venue(cfg: Config):
    if cfg.venue == "alpaca":
        return AlpacaVenue.from_credentials(
            api_key=cfg.alpaca_api_key,
            api_secret=cfg.alpaca_api_secret,
            paper=cfg.alpaca_paper,
        )
    if cfg.venue == "coinbase":
        return CoinbaseVenue.from_credentials(
            api_key=cfg.coinbase_api_key,
            api_secret=cfg.coinbase_api_secret,
            sandbox=cfg.coinbase_sandbox,
        )
    if cfg.venue == "fake":
        return FakeVenue()
    raise ValueError(f"Unsupported venue: {cfg.venue}")


def main(argv: Sequence[str] | None = None) -> int:
    del argv

    cfg = load_config()
    require_credentials(cfg)

    venue = _build_venue(cfg)
    feed = build_feed(cfg)
    strategy = SMACrossoverStrategy(
        symbol=cfg.symbol,
        strategy_name="sma_crossover",
        fast_length=5,
        slow_length=20,
        quantity=cfg.order_qty,
    )
    router = SignalRouter(venue)
    runtime = BotRuntime(
        feed=feed,
        strategy=strategy,
        router=router,
        symbol=cfg.symbol,
        timeframe=cfg.timeframe,
        warmup_bars=20,
    )

    if os.getenv("RUN_FOREVER", "0").strip() in {"1", "true", "yes", "on"}:
        runtime.run_forever(sleep_seconds=1.0)
    else:
        runtime.run_once()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
