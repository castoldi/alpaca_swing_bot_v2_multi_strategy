import numpy as np
import pandas as pd
import strategy as S
from config import PARAMS, StrategyType

def _make_df():
    # 120-bar uptrend with noise so that RSI varies and trend_pullback signals fire.
    # Fixed seed for reproducibility.
    np.random.seed(42)
    n = 120
    t = np.arange(n)
    close = 80 + t * 0.5 + np.random.randn(n) * 1.5
    close = np.maximum(close, 80.0)
    open_ = close * 0.997   # bullish candles: open slightly below close
    idx = pd.date_range("2025-07-01", periods=n, freq="D")
    return pd.DataFrame({
        "open": open_,
        "high": close * 1.015,
        "low":  close * 0.985,
        "close": close,
        "volume": [1_000_000] * n,
    }, index=idx)

def test_backtest_ticker_emits_single_exit_trades():
    df = _make_df()
    df[["open", "high", "low", "close"]] *= 0.4
    trades = S.backtest_ticker(df, "TEST", pd.Timestamp("2025-07-01"),
                               PARAMS, StrategyType.TREND_PULLBACK)
    assert trades
    reasons = {t.exit_reason for t in trades}
    # One bracket per entry: SL/TP/time/end-of-data only, never partial TP legs.
    assert reasons <= {"take_profit", "stop_loss", "time_stop", "end_of_data"}
    assert all(t.shares > 0 for t in trades)
