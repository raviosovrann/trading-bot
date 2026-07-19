"""Native Coinbase REST warmup and WebSocket streaming feeds (#171).

No live network: the HTTP and WebSocket clients are injected.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tradingbot.coinbase_feed import CoinbaseCandleFeed, CoinbaseStreamFeed


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeHttp:
    """Records requests and replays a canned candles payload."""

    def __init__(self, payload: dict | None = None, status: int = 200) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._payload = payload if payload is not None else {"candles": []}
        self._status = status

    def get(self, url: str, params: dict | None = None, timeout: float = 0) -> _FakeResponse:
        self.calls.append((url, dict(params or {})))
        return _FakeResponse(self._payload, self._status)


def _rest_candle(start: int, close: float) -> dict[str, str]:
    return {
        "start": str(start),
        "low": str(close - 1),
        "high": str(close + 1),
        "open": str(close),
        "close": str(close),
        "volume": "1.5",
    }


class TestCoinbaseCandleFeed:
    def test_warmup_returns_oldest_first(self) -> None:
        """Verify the newest-first API payload is reversed for the runtime.

        The processor dedups on increasing timestamps, so oldest-first is not
        cosmetic — the wrong order would drop every bar but the first.
        """
        http = _FakeHttp({"candles": [
            _rest_candle(180, 103.0), _rest_candle(120, 102.0), _rest_candle(60, 101.0),
        ]})
        feed = CoinbaseCandleFeed(http=http)

        candles = feed.warmup_candles("BTC/USD", "1m", 10)

        assert [c.timestamp for c in candles] == [60_000, 120_000, 180_000]
        assert [c.close for c in candles] == [101.0, 102.0, 103.0]

    def test_warmup_requests_the_right_product_and_granularity(self) -> None:
        """Verify house symbol/timeframe are translated for Coinbase."""
        http = _FakeHttp({"candles": [_rest_candle(60, 100.0)]})
        feed = CoinbaseCandleFeed(http=http)

        feed.warmup_candles("eth/usd", "5m", 3)

        url, params = http.calls[0]
        assert "ETH-USD" in url
        assert params["granularity"] == "FIVE_MINUTE"
        assert int(params["end"]) > int(params["start"])

    def test_warmup_honours_the_limit(self) -> None:
        """Verify only the newest ``limit`` candles are returned."""
        http = _FakeHttp({"candles": [_rest_candle(60 * i, float(i)) for i in range(20, 0, -1)]})
        feed = CoinbaseCandleFeed(http=http)

        candles = feed.warmup_candles("BTC/USD", "1m", 5)

        assert len(candles) == 5
        assert [c.close for c in candles] == [16.0, 17.0, 18.0, 19.0, 20.0]

    def test_malformed_rows_are_skipped(self) -> None:
        """Verify one bad row does not lose the whole warmup."""
        http = _FakeHttp({"candles": [
            {"start": "not-a-number", "open": "1", "high": "1", "low": "1", "close": "1", "volume": "1"},
            _rest_candle(60, 100.0),
        ]})
        feed = CoinbaseCandleFeed(http=http)

        candles = feed.warmup_candles("BTC/USD", "1m", 10)

        assert [c.close for c in candles] == [100.0]

    def test_latest_closed_candle(self) -> None:
        """Verify the convenience accessor returns the newest bar."""
        http = _FakeHttp({"candles": [_rest_candle(120, 102.0), _rest_candle(60, 101.0)]})
        feed = CoinbaseCandleFeed(http=http)

        assert feed.latest_closed_candle("BTC/USD", "1m").close == 102.0  # type: ignore[union-attr]

    def test_an_unsupported_timeframe_is_refused(self) -> None:
        """Verify an unmappable timeframe fails loudly."""
        feed = CoinbaseCandleFeed(http=_FakeHttp())
        with pytest.raises(ValueError, match="timeframe"):
            feed.warmup_candles("BTC/USD", "3s", 5)


class _FakeSocket:
    """Scripted WebSocket: replays frames, then blocks until closed."""

    def __init__(self, frames: list[str]) -> None:
        self.sent: list[dict] = []
        self._frames = list(frames)
        self.closed = False
        self._drained = asyncio.Event()

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def recv(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        self._drained.set()
        await asyncio.Event().wait()  # idle until cancelled
        raise AssertionError("unreachable")

    async def close(self) -> None:
        self.closed = True

    async def wait_drained(self) -> None:
        await self._drained.wait()


def _trades_frame(trades: list[dict], sequence: int = 1) -> str:
    return json.dumps({
        "channel": "market_trades",
        "sequence_num": sequence,
        "events": [{"type": "update", "trades": trades}],
    })


def _trade(trade_id: str, ts: float, price: float, size: float = 1.0) -> dict:
    return {
        "product_id": "BTC-USD", "trade_id": trade_id, "price": str(price),
        "size": str(size), "time": ts, "side": "BUY",
    }


def _feed_with(frames: list[str], clock) -> tuple[CoinbaseStreamFeed, _FakeSocket]:
    socket = _FakeSocket(frames)

    async def connect(url: str):
        return socket

    # A short tick keeps the tests fast; production defaults to one second.
    return (
        CoinbaseStreamFeed(timeframe="1m", connect=connect, clock=clock, tick_seconds=0.02),
        socket,
    )


class TestCoinbaseStreamFeed:
    @pytest.mark.asyncio
    async def test_subscribes_to_trades_and_heartbeats(self) -> None:
        """Verify both channels are requested for the product.

        Heartbeats keep the connection alive and carry the counter used to
        notice a stalled stream.
        """
        feed, socket = _feed_with([], clock=lambda: 0.0)
        task = asyncio.create_task(feed.run_async("BTC/USD"))
        await asyncio.sleep(0.05)

        channels = {msg["channel"] for msg in socket.sent}
        assert channels == {"market_trades", "heartbeats"}
        assert all(msg["product_ids"] == ["BTC-USD"] for msg in socket.sent)
        assert all(msg["type"] == "subscribe" for msg in socket.sent)

        feed.stop()
        await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_streams_a_closed_candle_to_the_handler(self) -> None:
        """Verify trades become a candle once their interval closes."""
        now = 60.0
        frames = [_trades_frame([_trade("1", 60.0, 100.0), _trade("2", 90.0, 110.0)])]
        feed, _socket = _feed_with(frames, clock=lambda: now)
        received: list = []
        feed.on_bar_for("BTC/USD", received.append)

        task = asyncio.create_task(feed.run_async("BTC/USD"))
        await asyncio.sleep(0.05)
        assert received == [], "the interval is still forming"

        now = 130.0  # the 60-120 interval has now closed
        await asyncio.sleep(0.15)

        assert len(received) == 1
        assert received[0].open == 100.0
        assert received[0].close == 110.0

        feed.stop()
        await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_a_sequence_gap_is_reported(self) -> None:
        """Verify a skipped ``sequence_num`` is surfaced, not ignored.

        This is the failure the ccxt feed could never see: the socket is alive
        and delivering, but messages went missing.
        """
        frames = [
            _trades_frame([_trade("1", 60.0, 100.0)], sequence=1),
            _trades_frame([_trade("2", 61.0, 101.0)], sequence=7),
        ]
        gaps: list[str] = []
        feed, _socket = _feed_with(frames, clock=lambda: 60.0)
        feed.on_gap(gaps.append)

        task = asyncio.create_task(feed.run_async("BTC/USD"))
        await asyncio.sleep(0.1)

        assert gaps, "a sequence gap must be reported"
        assert "missed 5" in gaps[0], gaps[0]
        assert "1 -> 7" in gaps[0], gaps[0]

        feed.stop()
        await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_contiguous_sequence_numbers_report_nothing(self) -> None:
        """Verify an orderly stream raises no false alarm."""
        frames = [
            _trades_frame([_trade("1", 60.0, 100.0)], sequence=1),
            _trades_frame([_trade("2", 61.0, 101.0)], sequence=2),
        ]
        gaps: list[str] = []
        feed, _socket = _feed_with(frames, clock=lambda: 60.0)
        feed.on_gap(gaps.append)

        task = asyncio.create_task(feed.run_async("BTC/USD"))
        await asyncio.sleep(0.1)

        assert gaps == []
        feed.stop()
        await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_stop_closes_the_socket(self) -> None:
        """Verify stopping releases the connection rather than leaking it."""
        feed, socket = _feed_with([], clock=lambda: 0.0)
        task = asyncio.create_task(feed.run_async("BTC/USD"))
        await asyncio.sleep(0.05)

        feed.stop()
        await asyncio.gather(task, return_exceptions=True)

        assert socket.closed

    @pytest.mark.asyncio
    async def test_stop_symbol_leaves_other_symbols_running(self) -> None:
        """Verify per-symbol lifecycle, as #112 requires of every feed."""
        feed, _socket = _feed_with([], clock=lambda: 0.0)
        btc = asyncio.create_task(feed.run_async("BTC/USD"))
        eth = asyncio.create_task(feed.run_async("ETH/USD"))
        await asyncio.sleep(0.05)

        feed.stop_symbol("BTC/USD")
        await asyncio.sleep(0.05)

        assert btc.done(), "the stopped symbol's loop should have exited"
        assert not eth.done(), "the other symbol must keep streaming"

        feed.stop()
        await asyncio.gather(btc, eth, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_a_malformed_frame_does_not_kill_the_stream(self) -> None:
        """Verify unparseable input is skipped rather than ending the feed."""
        frames = ["not json at all", _trades_frame([_trade("1", 60.0, 100.0)])]
        now = 60.0
        feed, _socket = _feed_with(frames, clock=lambda: now)
        received: list = []
        feed.on_bar_for("BTC/USD", received.append)

        task = asyncio.create_task(feed.run_async("BTC/USD"))
        await asyncio.sleep(0.05)
        now = 130.0
        await asyncio.sleep(0.15)

        assert len(received) == 1
        feed.stop()
        await asyncio.gather(task, return_exceptions=True)


class TestCoinbaseRequestLimits:
    """Coinbase caps a single candles request at 350 rows (verified live)."""

    def test_a_large_warmup_is_clamped_to_the_api_cap(self) -> None:
        """Verify we never ask for more candles than Coinbase will return.

        Exceeding the cap is a 400, so an unclamped request would turn a large
        warmup into a confusing start failure rather than a short warmup.
        """
        http = _FakeHttp({"candles": [_rest_candle(60 * i, float(i)) for i in range(1, 351)]})
        feed = CoinbaseCandleFeed(http=http)

        feed.warmup_candles("BTC/USD", "1m", 1000)

        _url, params = http.calls[0]
        window = int(params["end"]) - int(params["start"])
        assert window <= 350 * 60, f"requested {window / 60:.0f} minutes, over the 350 cap"

    def test_a_normal_warmup_is_not_clamped(self) -> None:
        """Verify the usual 220-bar warmup still asks for everything it needs."""
        http = _FakeHttp({"candles": []})
        feed = CoinbaseCandleFeed(http=http)

        feed.warmup_candles("BTC/USD", "1m", 220)

        _url, params = http.calls[0]
        window = int(params["end"]) - int(params["start"])
        assert window >= 220 * 60, "the requested window must cover the asked-for bars"

    def test_four_hour_is_supported(self) -> None:
        """FOUR_HOUR is a real Coinbase granularity and must be usable."""
        from tradingbot.coinbase_feed import bucket_seconds, granularity

        assert granularity("4h") == "FOUR_HOUR"
        assert bucket_seconds("4h") == 14_400
