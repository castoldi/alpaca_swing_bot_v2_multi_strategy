import pandas as pd

from config import PARAMS
from strategies import REGISTRY
from strategies.base import add_indicators


def frame(closes):
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="D")
    values = pd.Series(closes, index=idx, dtype=float)
    return add_indicators(pd.DataFrame({
        "open": values,
        "high": values + 1,
        "low": values - 1,
        "close": values,
        "volume": 1_000_000,
    }), PARAMS)


def test_sma_50_cross_enters_only_on_fresh_cross_above():
    strat = REGISTRY["sma_50_cross"]
    df = frame([100.0] * 50 + [101.0, 102.0])
    assert strat.check_entry(df, 50, PARAMS) is not None
    assert strat.check_entry(df, 51, PARAMS) is None


def test_sma_50_cross_exits_only_on_fresh_cross_below():
    strat = REGISTRY["sma_50_cross"]
    df = frame([100.0] * 49 + [101.0, 99.0, 98.0])
    assert strat.check_exit(df, 50, PARAMS) == "sma_cross_down"
    assert strat.check_exit(df, 51, PARAMS) is None


def test_sma_50_cross_needs_a_complete_sma_and_has_daily_metadata():
    strat = REGISTRY["sma_50_cross"]
    assert strat.check_entry(frame([100.0] * 49), 48, PARAMS) is None
    assert strat.timeframe == "1d"
    assert strat.exit_mode == "signal_with_stop"
    assert strat.has_take_profit is False
