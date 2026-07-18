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
