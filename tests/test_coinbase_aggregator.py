"""Trade-to-candle aggregation for the native Coinbase feed (#171).

These are the bar-close semantics the whole feed rests on: a candle must be
emitted exactly once, when its interval has closed, and the forming interval
must never escape.
"""

from __future__ import annotations

import pytest

from tradingbot.coinbase_feed import (
    TradeAggregator,
    bucket_seconds,
    to_product_id,
    to_symbol,
)


def _trade(trade_id: str, ts: float, price: float, size: float) -> dict[str, object]:
    """Build a raw Coinbase ``market_trades`` entry."""
    return {
        "product_id": "BTC-USD",
        "trade_id": trade_id,
        "price": str(price),
        "size": str(size),
        "time": ts,
        "side": "BUY",
    }


class TestSymbolMapping:
    @pytest.mark.parametrize(
        ("symbol", "product"),
        [("BTC/USD", "BTC-USD"), ("eth/usd", "ETH-USD"), ("BTC-USD", "BTC-USD")],
    )
    def test_symbol_to_product_id(self, symbol: str, product: str) -> None:
        """Verify house symbols map onto Coinbase product ids."""
        assert to_product_id(symbol) == product

    def test_product_id_back_to_symbol(self) -> None:
        """Verify the mapping round-trips, so handlers key consistently."""
        assert to_symbol("BTC-USD") == "BTC/USD"
        assert to_symbol(to_product_id("ETH/USD")) == "ETH/USD"


class TestTimeframes:
    @pytest.mark.parametrize(
        ("timeframe", "seconds"),
        [("1m", 60), ("5m", 300), ("15m", 900), ("1h", 3600), ("1d", 86_400)],
    )
    def test_supported_timeframes(self, timeframe: str, seconds: int) -> None:
        """Verify each supported timeframe maps to its interval length."""
        assert bucket_seconds(timeframe) == seconds

    def test_unknown_timeframe_is_refused(self) -> None:
        """An unmapped timeframe must fail loudly, not silently bucket wrongly."""
        with pytest.raises(ValueError, match="timeframe"):
            bucket_seconds("3s")


class TestAggregation:
    def test_a_closed_interval_becomes_one_candle(self) -> None:
        """Verify OHLCV is derived correctly from the trades in an interval."""
        agg = TradeAggregator(bucket=60)
        for t in (
            _trade("1", 60.0, 100.0, 1.0),
            _trade("2", 70.0, 105.0, 2.0),
            _trade("3", 80.0, 95.0, 3.0),
            _trade("4", 90.0, 102.0, 4.0),
        ):
            agg.add(t)

        candles = agg.close_elapsed(now=125.0)

        assert len(candles) == 1
        candle = candles[0]
        assert candle.timestamp == 60_000, "timestamps are epoch milliseconds"
        assert candle.open == 100.0
        assert candle.high == 105.0
        assert candle.low == 95.0
        assert candle.close == 102.0
        assert candle.volume == pytest.approx(10.0)

    def test_the_forming_interval_is_never_emitted(self) -> None:
        """Verify an interval still in progress is withheld.

        Emitting it would hand the strategy a bar whose close is not final.
        """
        agg = TradeAggregator(bucket=60)
        agg.add(_trade("1", 60.0, 100.0, 1.0))

        assert agg.close_elapsed(now=90.0) == [], "mid-interval must stay withheld"
        assert agg.close_elapsed(now=119.999) == []
        assert len(agg.close_elapsed(now=120.0)) == 1

    def test_a_candle_is_emitted_exactly_once(self) -> None:
        """Verify repeated ticks do not re-emit a closed interval."""
        agg = TradeAggregator(bucket=60)
        agg.add(_trade("1", 60.0, 100.0, 1.0))

        first = agg.close_elapsed(now=130.0)
        assert len(first) == 1
        assert agg.close_elapsed(now=200.0) == []
        assert agg.close_elapsed(now=999.0) == []

    def test_intervals_without_trades_are_skipped_not_invented(self) -> None:
        """Verify quiet intervals produce no candle.

        Synthesizing flat bars would fabricate market activity that never
        happened — on an illiquid pair that could badly mislead a strategy.
        """
        agg = TradeAggregator(bucket=60)
        agg.add(_trade("1", 60.0, 100.0, 1.0))
        agg.add(_trade("2", 300.0, 110.0, 1.0))

        candles = agg.close_elapsed(now=400.0)

        assert [c.timestamp for c in candles] == [60_000, 300_000]

    def test_candles_are_emitted_oldest_first(self) -> None:
        """Verify ordering, since the processor dedups on increasing timestamps."""
        agg = TradeAggregator(bucket=60)
        for i, ts in enumerate((60.0, 120.0, 180.0)):
            agg.add(_trade(str(i), ts, 100.0 + i, 1.0))

        candles = agg.close_elapsed(now=300.0)
        assert [c.timestamp for c in candles] == [60_000, 120_000, 180_000]

    def test_duplicate_trade_ids_are_counted_once(self) -> None:
        """Verify the snapshot/update overlap cannot double-count volume.

        The channel replays recent trades in its snapshot, so the same trade
        arrives twice on a reconnect.
        """
        agg = TradeAggregator(bucket=60)
        agg.add(_trade("dup", 60.0, 100.0, 5.0))
        agg.add(_trade("dup", 60.0, 100.0, 5.0))

        candles = agg.close_elapsed(now=130.0)
        assert candles[0].volume == pytest.approx(5.0)

    def test_a_trade_arriving_after_its_interval_closed_is_dropped(self) -> None:
        """Verify a late trade cannot resurrect or mutate a published candle.

        Re-emitting a bar the strategy already acted on would let it trade the
        same interval twice.
        """
        agg = TradeAggregator(bucket=60)
        agg.add(_trade("1", 60.0, 100.0, 1.0))
        emitted = agg.close_elapsed(now=130.0)
        assert len(emitted) == 1

        agg.add(_trade("late", 65.0, 999.0, 1.0))

        assert agg.close_elapsed(now=200.0) == []

    def test_out_of_order_trades_within_an_open_interval_are_kept(self) -> None:
        """Verify open/close use trade time, not arrival order.

        Coinbase sends its snapshot newest-first, so arrival order is not
        chronological.
        """
        agg = TradeAggregator(bucket=60)
        agg.add(_trade("late", 90.0, 105.0, 1.0))
        agg.add(_trade("early", 65.0, 100.0, 1.0))

        candle = agg.close_elapsed(now=130.0)[0]
        assert candle.open == 100.0, "open is the earliest trade in the interval"
        assert candle.close == 105.0, "close is the latest trade in the interval"

    def test_trade_times_may_be_iso_strings(self) -> None:
        """Verify the wire format (RFC 3339 strings) is accepted."""
        agg = TradeAggregator(bucket=60)
        agg.add({
            "product_id": "BTC-USD", "trade_id": "1", "price": "100.0",
            "size": "1.0", "time": "2026-07-19T00:01:05.500000Z", "side": "BUY",
        })

        candles = agg.close_elapsed(now=1_784_419_400.0)
        assert len(candles) == 1
        assert candles[0].timestamp % 60_000 == 0, "bucket start is interval-aligned"

    def test_malformed_trades_are_ignored(self) -> None:
        """Verify one bad row cannot break the stream for every bot."""
        agg = TradeAggregator(bucket=60)
        agg.add({"trade_id": "bad", "price": "not-a-number", "size": "1", "time": 60.0})
        agg.add({"trade_id": "no-time", "price": "1", "size": "1"})
        agg.add(_trade("good", 60.0, 100.0, 1.0))

        candles = agg.close_elapsed(now=130.0)
        assert len(candles) == 1
        assert candles[0].close == 100.0
