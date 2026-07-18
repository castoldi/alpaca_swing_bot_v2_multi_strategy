from datetime import date

import pandas as pd

from strategies import REGISTRY


def test_strategy_timeframes_are_mixed_without_changing_global_default():
    assert REGISTRY["trend_pullback"].timeframe == "4h"
    assert REGISTRY["sma_50_cross"].timeframe == "1d"


def test_sma_cross_report_metadata():
    from backtest_2025 import STRATEGY_COLORS
    from build_report_2025 import params_html_for_strategy
    from config import StrategyType

    assert "sma_50_cross" in STRATEGY_COLORS
    text = params_html_for_strategy(StrategyType.SMA_50_CROSS)
    assert "Daily" in text
    assert "SMA(50)" in text
    assert "No take profit" in text


def test_report_accepts_history_label_and_source():
    from build_report_2025 import build_report_2025

    html = build_report_2025(
        {},
        {},
        None,
        report_label="2016–Present Backtest",
        data_source="Alpaca SIP historical data",
    )

    assert "2016–Present Backtest" in html
    assert "Alpaca SIP historical data" in html
    assert "yfinance live data" not in html


def test_backtest_history_uses_sip_cache_and_drops_current_daily(monkeypatch):
    import backtest_2025

    today = pd.Timestamp.now(tz="America/New_York").date()
    index = pd.to_datetime([today - pd.Timedelta(days=1), today])
    bars = pd.DataFrame(
        {
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [1_000, 1_100],
        },
        index=index,
    )

    calls = []

    class FakeCache:
        def get_bars(self, *args, **kwargs):
            calls.append((args, kwargs))
            return bars

    monkeypatch.setattr(backtest_2025, "_MARKET_CACHE", FakeCache())

    result = backtest_2025.download_history(
        "AMD", date(2026, 1, 1), date(2026, 12, 31), timeframe="1d"
    )

    assert calls[0][1]["feed"] == "sip"
    assert list(result.index.date) == [today - pd.Timedelta(days=1)]
