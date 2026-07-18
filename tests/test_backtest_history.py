from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from strategies import Trade

import backtest_history


def make_trade(
    *,
    ticker: str = "AMD",
    entry: str,
    exit: str,
    pnl: float,
    strategy: str = "trend_pullback",
) -> Trade:
    return Trade(
        ticker=ticker,
        entry_date=pd.Timestamp(entry),
        entry_price=100.0,
        stop_loss=90.0,
        take_profit=110.0,
        exit_date=pd.Timestamp(exit),
        exit_price=100.0 + pnl,
        exit_reason="time_stop",
        bars_held=1,
        shares=1.0,
        pnl_dollars=pnl,
        pnl_pct=pnl / 100.0,
        strategy=strategy,
    )


def test_rejects_history_before_alpaca_boundary():
    with pytest.raises(ValueError, match="2016-01-01"):
        backtest_history.validate_range(
            date(2015, 12, 31), date(2016, 2, 1)
        )


def test_rejects_reversed_history_range():
    with pytest.raises(ValueError, match="before"):
        backtest_history.validate_range(
            date(2020, 2, 1), date(2020, 1, 1)
        )


def test_yearly_stats_group_accepted_trades_by_exit_year():
    trades = [
        make_trade(entry="2020-12-31", exit="2021-01-04", pnl=10.0),
        make_trade(entry="2021-06-01", exit="2021-06-02", pnl=-4.0),
    ]

    result = backtest_history.yearly_stats(trades, 2020, 2021)

    assert result[2020]["trades"] == 0
    assert result[2021]["trades"] == 2
    assert result[2021]["total_pnl"] == 6.0


def test_run_history_includes_partial_ticker_histories(tmp_path, monkeypatch):
    old_bars = pd.DataFrame(
        {
            "open": [100.0] * 60,
            "high": [101.0] * 60,
            "low": [99.0] * 60,
            "close": [100.0] * 60,
            "volume": [1_000.0] * 60,
        },
        index=pd.date_range("2016-01-04", periods=60, freq="D"),
    )
    calls = []

    def fake_download(ticker, start, end, timeframe):
        calls.append((ticker, start, end, timeframe))
        return old_bars if ticker == "OLD" else old_bars.iloc[0:0]

    def fake_backtest(frame, ticker, window_start, params, strategy):
        assert ticker == "OLD"
        assert window_start == pd.Timestamp("2016-01-01")
        return [
            make_trade(
                ticker=ticker,
                entry="2016-02-01",
                exit="2016-02-02",
                pnl=5.0,
                strategy=strategy.name,
            )
        ]

    class FakeCache:
        @staticmethod
        def status():
            return [{"symbol": "OLD", "bar_count": 60}]

    html_path = tmp_path / "history.html"
    json_path = tmp_path / "history.json"
    monkeypatch.setattr(backtest_history, "TICKERS", ["OLD", "NEW"])
    monkeypatch.setattr(
        backtest_history,
        "get_enabled",
        lambda: [SimpleNamespace(name="trend_pullback", timeframe="4h")],
    )
    monkeypatch.setattr(backtest_history, "download_history", fake_download)
    monkeypatch.setattr(backtest_history, "backtest_ticker", fake_backtest)
    monkeypatch.setattr(backtest_history, "_MARKET_CACHE", FakeCache())
    monkeypatch.setattr(backtest_history, "OUTPUT_HTML", html_path)
    monkeypatch.setattr(backtest_history, "OUTPUT_JSON", json_path)
    monkeypatch.setattr(
        backtest_history,
        "build_report_2025",
        lambda *args, **kwargs: kwargs["report_label"],
    )

    result = backtest_history.run_history_backtest(
        date(2016, 1, 1), date(2016, 12, 31)
    )

    assert {call[0] for call in calls} == {"OLD", "NEW"}
    assert result["strategies"]["trend_pullback"]["cumulative"]["trades"] == 1
    assert result["strategies"]["trend_pullback"]["yearly"]["2016"]["total_pnl"] == 5.0
    assert result["actual_start"].startswith("2016-01-04")
    assert result["actual_end"].startswith("2016-03-03")
    assert result["cache"][0]["symbol"] == "OLD"
    assert "2016" in html_path.read_text(encoding="utf-8")
    assert json_path.exists()
