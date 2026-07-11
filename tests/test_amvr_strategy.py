from tradingbot.amvr import AdaptiveMomentumRibbonStrategy, _hma_series, _wma
from tradingbot.models import Action, Candle, PositionSide


# --------------------------------------------------------------------------- #
# math primitives
# --------------------------------------------------------------------------- #

def test_wma_weights_recent_highest():
    # weights 1,2,3 over oldest->newest: (1*1 + 2*2 + 3*3) / (1+2+3) = 14/6
    assert _wma([1.0, 2.0, 3.0]) == 14.0 / 6.0


def test_wma_single_value():
    assert _wma([5.0]) == 5.0


def test_hma_series_leading_none_then_tracks_uptrend():
    closes = [float(i) for i in range(1, 61)]  # strictly rising
    hma = _hma_series(closes, 10)
    assert hma[0] is None  # not enough data early
    warm = [h for h in hma if h is not None]
    assert len(warm) > 5
    assert warm[-1] > warm[-5]  # rises with the series


# --------------------------------------------------------------------------- #
# price-series builders (chosen to drive known velocity/acceleration states)
# --------------------------------------------------------------------------- #

def _down(n, base=100.0, rate=0.01):
    return [base * ((1 - rate) ** i) for i in range(n)]


def _expup(n, base, rate=0.03):
    # exponential rise -> velocity positive AND increasing (acceleration > 0)
    return [base * ((1 + rate) ** i) for i in range(1, n + 1)]


def _linup(n, base, step):
    return [base + step * i for i in range(1, n + 1)]


# Base series where ALL entry conditions align: a dip (which produces a bullish
# "prepare" crossover) followed by a long, accelerating rise that turns all
# three ribbons green.
def _bullish_base():
    d = _down(60)
    return d + _expup(120, d[-1])


# Rise then a short sharp drop; the fast-HMA velocity crosses below zero exactly
# on the final bar -> bearish "prepare" (exit) signal.
def _reversal_base():
    up = _expup(160, 100.0)
    return up + _down(7, up[-1], rate=0.03)


# Dip then a gentle linear rise: fast ribbon greens but the slow ribbon has not
# (velocity of the slow HMA still <= 0) -> "not all green".
def _partial_green_base():
    d = _down(120)
    return d + _linup(8, d[-1], 0.6)


def _candles(closes):
    return [
        Candle(timestamp=i * 3600_000, open=c, high=c, low=c, close=c, volume=1.0)
        for i, c in enumerate(closes)
    ]


class _FakeMtfFeed:
    """Returns canned closes per timeframe; records fetch calls."""

    def __init__(self, closes_by_tf):
        self._by_tf = closes_by_tf
        self.calls = []

    def warmup_candles(self, symbol, timeframe, limit):
        self.calls.append((symbol, timeframe, limit))
        return _candles(self._by_tf.get(timeframe, []))


def _make(tf_closes, **kw):
    feed = _FakeMtfFeed(tf_closes)
    strat = AdaptiveMomentumRibbonStrategy(
        symbol="XRP/USD",
        mtf_feed=feed,
        mtf_bars=40,
        mtf_cache_seconds=0.0,  # always fetch in tests
        **kw,
    )
    return strat, feed


_HTF_BULL = {"1h": _expup(40, 100.0), "4h": _expup(40, 100.0)}


# --------------------------------------------------------------------------- #
# entry / exit behaviour
# --------------------------------------------------------------------------- #

def test_enters_long_when_all_conditions_align():
    strat, feed = _make(_HTF_BULL)
    sig = strat.on_bar(_candles(_bullish_base()))
    assert sig is not None
    assert sig.action is Action.buy
    assert sig.position_side is PositionSide.long
    assert sig.symbol == "XRP/USD"
    tfs = {c[1] for c in feed.calls}
    assert "1h" in tfs and "4h" in tfs


def test_no_entry_when_4h_not_accelerating():
    htf = {"1h": _expup(40, 100.0), "4h": _down(40)}  # 4h falling
    strat, _ = _make(htf)
    assert strat.on_bar(_candles(_bullish_base())) is None


def test_no_entry_when_ribbons_not_all_green():
    strat, _ = _make(_HTF_BULL)
    assert strat.on_bar(_candles(_partial_green_base())) is None


def test_edge_triggered_does_not_rebuy_while_in_position():
    strat, _ = _make(_HTF_BULL)
    base = _candles(_bullish_base())
    first = strat.on_bar(base)
    second = strat.on_bar(base)
    assert first is not None and first.action is Action.buy
    assert second is None  # already long


def test_exits_on_bearish_prepare_when_in_position():
    strat, _ = _make(_HTF_BULL)
    strat._in_position = True  # pretend we already hold a long
    sig = strat.on_bar(_candles(_reversal_base()))
    assert sig is not None
    assert sig.action is Action.close
    assert sig.position_side is PositionSide.flat


def test_returns_none_on_insufficient_history():
    strat, _ = _make(_HTF_BULL)
    assert strat.on_bar(_candles(_expup(20, 100.0))) is None


def test_exit_does_not_fetch_mtf():
    # exits are base-timeframe only; no HTF fetch needed
    strat, feed = _make({"1h": [], "4h": []})
    strat._in_position = True
    strat.on_bar(_candles(_reversal_base()))
    assert feed.calls == []
