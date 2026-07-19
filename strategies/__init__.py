"""Strategy registry — single source of truth for all strategies.

Toggle a strategy on/off by editing ENABLED_STRATEGIES in config.py.
"""
from __future__ import annotations

from config import ENABLED_STRATEGIES
from .base import (
    BaseStrategy, EntrySignal, Trade, ExitLeg,
    add_indicators, add_earnings_filter, backtest_ticker, backtest_signal_exit_ticker,
    simulate_exit, simulate_exit_scaleout,
    is_tp_reachable_in_days, split_take_profit, split_qty,
    SKIP_EARNINGS_STRATEGIES,
)
from .trend_pullback import TrendPullbackStrategy
from .breakout import BreakoutStrategy
from .mean_reversion import MeanReversionStrategy
from .momentum_macd import MomentumMACDStrategy
from .regime_adaptive import RegimeAdaptiveStrategy
from .sma_50_cross import SMA50CrossStrategy
from .ensemble import EnsembleStrategy

# Canonical ordered registry — insertion order = display order in UI.
REGISTRY: dict[str, BaseStrategy] = {
    "trend_pullback": TrendPullbackStrategy(),
    "breakout":       BreakoutStrategy(),
    "mean_reversion": MeanReversionStrategy(),
    "momentum_macd":  MomentumMACDStrategy(),
    "regime":         RegimeAdaptiveStrategy(),
    "sma_50_cross":   SMA50CrossStrategy(),
    "ensemble":       EnsembleStrategy(),
}

# Apply enabled/disabled from config.
for _name, _strat in REGISTRY.items():
    _strat.enabled = _name in ENABLED_STRATEGIES


def get_strategy(name: str) -> BaseStrategy:
    """Look up a strategy by its string name (raises KeyError if unknown)."""
    return REGISTRY[name]


def get_all() -> list[BaseStrategy]:
    return list(REGISTRY.values())


def get_enabled() -> list[BaseStrategy]:
    return [s for s in REGISTRY.values() if s.enabled]


_PORTFOLIO_EXPORTS = {
    "BacktestCandidate",
    "collect_backtest_candidates",
    "materialize_candidate",
}


def __getattr__(name: str):
    """Lazily export portfolio APIs without a package import cycle."""
    if name in _PORTFOLIO_EXPORTS:
        import backtest_portfolio

        value = getattr(backtest_portfolio, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
