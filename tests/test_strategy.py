from tradingbot.models import Action, Candle, PositionSide
from tradingbot.strategy import SMACrossoverStrategy


def _candles_from_closes(closes: list[float]):
    candles = []
    for i, close in enumerate(closes, start=1):
        candles.append(
            Candle(
                timestamp=i,
                open=close,
                high=close,
                low=close,
                close=close,
                volume=1.0,
            )
        )
    return candles


def test_sma_strategy_no_signal_when_insufficient_bars():
    strategy = SMACrossoverStrategy(symbol="BTC/USD", fast_length=2, slow_length=3, quantity=0.01)
    candles = _candles_from_closes([10, 11, 12])
    assert strategy.on_bar(candles) is None


def test_sma_strategy_emits_buy_on_cross_up():
    strategy = SMACrossoverStrategy(symbol="BTC/USD", fast_length=2, slow_length=3, quantity=0.02)
    candles = _candles_from_closes([10, 9, 8, 12])

    sig = strategy.on_bar(candles)

    assert sig is not None
    assert sig.action is Action.buy
    assert sig.position_side is PositionSide.long
    assert sig.quantity == 0.02
    assert sig.symbol == "BTC/USD"


def test_sma_strategy_emits_sell_on_cross_down():
    strategy = SMACrossoverStrategy(symbol="BTC/USD", fast_length=2, slow_length=3, quantity=0.02)
    candles = _candles_from_closes([8, 9, 10, 6])

    sig = strategy.on_bar(candles)

    assert sig is not None
    assert sig.action is Action.sell
    assert sig.position_side is PositionSide.flat
