import pandas as pd
import backtest_2025 as B
from strategy import Trade


def _t(reason, pnl):
    return Trade(ticker="X", entry_date=pd.Timestamp("2026-01-02"), entry_price=100.0,
                 stop_loss=90.0, take_profit=108.0, exit_date=pd.Timestamp("2026-01-05"),
                 exit_price=104.0, exit_reason=reason, bars_held=3, shares=1.0,
                 pnl_dollars=pnl, pnl_pct=pnl / 100.0, strategy="trend_pullback")


def test_tp_legs_counted_as_take_profit():
    trades = [_t("tp1", 2), _t("tp2", 3), _t("tp3", 4), _t("stop_loss", -5), _t("time_stop", 1)]
    s = B.compute_stats(trades)
    assert s["tp_count"] == 3
    assert s["sl_count"] == 1
    assert s["time_count"] == 1


def test_max_drawdown_uses_running_equity_peak():
    gain = _t("take_profit", 100)
    loss = _t("stop_loss", -55)
    gain.exit_date = pd.Timestamp("2026-01-03")
    loss.exit_date = pd.Timestamp("2026-01-04")

    assert B.compute_max_drawdown([gain, loss], initial_equity=1_000.0) == 0.05
