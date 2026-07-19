"""Compatibility shim — real strategy code lives in the strategies/ package.

All existing imports (bot.py, backtest_*.py, etc.) continue to work unchanged.
"""
from strategies.base import (
    EntrySignal, Trade, ExitLeg,
    add_indicators, add_earnings_filter,
    simulate_exit, simulate_exit_scaleout,
    is_tp_reachable_in_days, split_take_profit, split_qty,
    SKIP_EARNINGS_STRATEGIES, _max_holding_days,
)
from strategies import REGISTRY
from backtest_portfolio import (
    BacktestCandidate,
    collect_backtest_candidates,
    materialize_candidate,
)


def get_entry_checker(strategy):
    """Return the check_entry method for a StrategyType enum or string name."""
    from config import StrategyType
    name = strategy.value if isinstance(strategy, StrategyType) else str(strategy)
    return REGISTRY[name].check_entry


def backtest_ticker(df, ticker, window_start, p=None, strategy=None):
    """Legacy wrapper: accepts StrategyType enum, string, or BaseStrategy instance."""
    from strategies.base import backtest_ticker as _bt, BaseStrategy
    from config import StrategyType, PARAMS as _PARAMS
    p = p or _PARAMS
    if not isinstance(strategy, BaseStrategy):
        from config import StrategyType
        name = strategy.value if isinstance(strategy, StrategyType) else str(strategy)
        strategy = REGISTRY[name]
    return _bt(df, ticker, window_start, p, strategy)


def check_entry(df, idx, p=None):
    from config import PARAMS as _PARAMS
    p = p or _PARAMS
    return get_entry_checker(p.strategy)(df, idx, p)


# Legacy dispatch table (kept for any code that references it directly)
ENTRY_CHECKERS = {name: s.check_entry for name, s in REGISTRY.items()}

run_ticker_backtest = backtest_ticker
