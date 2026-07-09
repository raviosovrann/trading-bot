from __future__ import annotations

from collections.abc import Sequence

from .config import Config, load_config, require_credentials
from .datafeed import build_feed
from .router import SignalRouter
from .runtime import BotRuntime, StreamRuntime
from .strategy import SMACrossoverStrategy
from .stream import build_stream_feed
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
        stream_feed = build_stream_feed(cfg)
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
    feed = build_feed(cfg)
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
