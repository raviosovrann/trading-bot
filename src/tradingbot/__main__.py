from __future__ import annotations

import logging
from collections.abc import Sequence

from .config import Config, load_config, require_credentials
from .datafeed import CcxtCandleFeed
from .router import SignalRouter
from .runtime import BotRuntime, StreamRuntime
from .stream import CcxtStreamFeed
from .strategies import StrategyContext, build_strategy
from .venues.ccxt import CcxtVenue

# Enough base bars to warm up the slowest ribbon (HMA 100 + velocity/accel).
_WARMUP_BARS = 220


def _build_venue(cfg: Config) -> CcxtVenue:
    return CcxtVenue.from_exchange(
        cfg.exchange,
        cfg.api_key,
        cfg.api_secret,
        cfg.api_password or None,
        live=cfg.live,
    )


def main(argv: Sequence[str] | None = None) -> int:
    del argv

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config()
    require_credentials(cfg)

    mode = "LIVE (real orders)" if cfg.live else "DRY-RUN (no orders sent)"
    logging.info(
        "starting: exchange=%s symbol=%s timeframe=%s stream=%s | %s",
        cfg.exchange, cfg.symbol, cfg.timeframe, cfg.stream, mode,
    )

    venue = _build_venue(cfg)

    # Dedicated REST feed the strategy uses to fetch 1H/4H higher-timeframe
    # momentum (independent of the base-timeframe feed/stream below).
    mtf_feed = CcxtCandleFeed.from_exchange(
        cfg.exchange,
        cfg.api_key,
        cfg.api_secret,
        cfg.api_password or None,
    )
    strategy = build_strategy(
        cfg.strategy,
        StrategyContext(
            symbol=cfg.symbol,
            timeframe=cfg.timeframe,
            quantity=cfg.order_qty,
            data_feed=mtf_feed,
            params={},
        ),
    )
    router = SignalRouter(venue)

    if cfg.stream:
        # Event-driven mode: block on the WebSocket feed, acting on each pushed
        # closed bar. Ctrl+C / SIGTERM triggers a graceful shutdown.
        stream_feed = CcxtStreamFeed.from_exchange(
            cfg.exchange,
            cfg.api_key,
            cfg.api_secret,
            cfg.api_password or None,
            timeframe=cfg.timeframe,
        )
        StreamRuntime(
            feed=stream_feed,
            strategy=strategy,
            router=router,
            symbol=cfg.symbol,
            timeframe=cfg.timeframe,
            warmup_bars=_WARMUP_BARS,
        ).start()
        return 0

    # Single-shot mode: process the latest closed candle over REST, then exit
    # (suitable for cron-style invocation).
    feed = CcxtCandleFeed.from_exchange(
        cfg.exchange,
        cfg.api_key,
        cfg.api_secret,
        cfg.api_password or None,
    )
    BotRuntime(
        feed=feed,
        strategy=strategy,
        router=router,
        symbol=cfg.symbol,
        timeframe=cfg.timeframe,
        warmup_bars=_WARMUP_BARS,
    ).run_once()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
