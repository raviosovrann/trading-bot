from __future__ import annotations

from collections.abc import Sequence

from .config import Config, load_config, require_credentials
from .datafeed import CcxtCandleFeed
from .router import SignalRouter
from .runtime import BotRuntime, StreamRuntime
from .strategy import SMACrossoverStrategy
from .stream import CcxtStreamFeed
from .venues.ccxt import CcxtVenue


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

    cfg = load_config()
    require_credentials(cfg)

    venue = _build_venue(cfg)
    strategy = SMACrossoverStrategy(
        symbol=cfg.symbol,
        strategy_name="sma_crossover",
        fast_length=5,
        slow_length=20,
        quantity=cfg.order_qty,
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
            warmup_bars=20,
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
        warmup_bars=20,
    ).run_once()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
