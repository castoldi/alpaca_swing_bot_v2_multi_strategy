import pandas as pd

from config import PARAMS
from strategies import REGISTRY, backtest_ticker


def bars(rows):
    df = pd.DataFrame(
        rows, index=pd.date_range("2026-01-01", periods=len(rows), freq="D")
    )
    df["volume"] = 1_000_000
    return df


def test_signal_exit_backtest_fills_next_open_and_exits_next_open():
    rows = []
    for _ in range(50):
        rows.append({"open": 100, "high": 101, "low": 99, "close": 100})
    rows += [
        {"open": 101, "high": 103, "low": 100, "close": 102},
        {"open": 103, "high": 104, "low": 102, "close": 103},
        {"open": 99, "high": 100, "low": 98, "close": 98},
        {"open": 97, "high": 98, "low": 96, "close": 97},
    ]
    trades = backtest_ticker(
        bars(rows), "TEST", pd.Timestamp("2026-01-01"),
        PARAMS, REGISTRY["sma_50_cross"],
    )
    assert len(trades) == 1
    assert trades[0].entry_price == 103
    assert trades[0].exit_price == 97
    assert trades[0].exit_reason == "sma_cross_down"
    assert trades[0].take_profit == 0.0


def test_signal_exit_backtest_honors_gap_through_stop_without_time_exit():
    rows = [
        {"open": 100, "high": 101, "low": 99, "close": 100}
        for _ in range(50)
    ]
    rows += [
        {"open": 101, "high": 103, "low": 100, "close": 102},
        {"open": 103, "high": 104, "low": 102, "close": 103},
        {"open": 90, "high": 92, "low": 89, "close": 91},
    ]
    trade = backtest_ticker(
        bars(rows), "TEST", pd.Timestamp("2026-01-01"),
        PARAMS, REGISTRY["sma_50_cross"],
    )[0]
    assert trade.exit_price == 90
    assert trade.exit_reason == "gap_stop"
