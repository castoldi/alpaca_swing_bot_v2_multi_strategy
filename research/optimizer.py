"""Research optimizer — parameter tuning via grid/random search for strategy improvement."""
from __future__ import annotations

import random
from datetime import date
from dataclasses import asdict, replace
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from config import PARAMS, TICKERS, StrategyParams, StrategyType
from logger_setup import get_logger
from strategy import Trade
from strategies import REGISTRY, strategy_universe
from research.optimizer_data import BacktestDataset
from research.significance import (
    SignificanceReport,
    evaluate_search,
    returns_from_trades,
    sharpe_ratio,
    t_statistic,
)

log = get_logger(__name__)
ROOT = Path(__file__).parent.parent
REPORTS_DIR = ROOT / "reports"


# ── Data helper ──────────────────────────────────────────────────────────────

def download_history(ticker: str, start: date, end: date, timeframe: str) -> pd.DataFrame:
    from backtest_2025 import download_history as shared_history
    return shared_history(ticker, start, end, timeframe)


def load_dataset(strategy: StrategyType, year: int) -> BacktestDataset:
    """Load one immutable market/earnings snapshot before any trial runs."""
    from backtest_execution import load_execution_bars
    from backtest_valuation import previous_session_close
    from earnings_calendar import get_store
    from strategies.base import SKIP_EARNINGS_STRATEGIES

    strategy_obj = REGISTRY[strategy.value]
    start, end = date(year, 1, 1), date(year, 12, 31)
    # Include the preceding session close for the first daily-loss baseline.
    minute_start = previous_session_close(pd.Timestamp(start) + pd.Timedelta(hours=12)) - pd.Timedelta(minutes=1)
    minute_end = pd.Timestamp(end) + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
    signals, execution, earnings = {}, {}, {}
    for ticker in strategy_universe(strategy_obj, TICKERS):
        frame = download_history(ticker, start, end, strategy_obj.timeframe)
        if frame.empty:
            continue
        signals[ticker, strategy_obj.timeframe] = frame
        minute = load_execution_bars(frame, ticker, minute_start, minute_end)
        minute.attrs.setdefault('timeframe', '1min')
        execution[ticker] = minute
        if strategy_obj.name in SKIP_EARNINGS_STRATEGIES:
            earnings[ticker] = get_store().history(ticker, minute_end)
    if not signals:
        raise ValueError('No signal data available for optimizer dataset')
    return BacktestDataset(strategy.value, year, signals, execution, earnings)


def run_backtest_for_params(
    p: StrategyParams, strategy: StrategyType, year: int,
    *, dataset: BacktestDataset | None = None,
) -> list[Trade]:
    """Use exactly the annual runner, including minute execution and risk policy."""
    from backtest_2025 import run_strategy_year
    dataset = load_dataset(strategy, year) if dataset is None else dataset
    if dataset.strategy != strategy.value or dataset.year != year:
        raise ValueError('Optimizer dataset does not match strategy/year')
    signals, execution, earnings = dataset.inputs()
    result, _, _ = run_strategy_year(
        signals, REGISTRY[strategy.value], year, p,
        execution_data=execution, earnings_store=earnings,
    )
    return list(result.trades)


def compute_stats(trades: list[Trade], initial_equity: float = PARAMS.initial_backtest_equity) -> dict[str, Any]:
    if not trades:
        return dict(trades=0, wins=0, losses=0, win_rate=0, total_pnl=0, profit_factor=0,
                    avg_pnl_pct=0, max_drawdown=0, sharpe=0.0, t_stat=0.0, p_value=1.0)

    wins = [t for t in trades if t.pnl_dollars > 0]
    losses = [t for t in trades if t.pnl_dollars <= 0]
    gross_profit = sum(t.pnl_dollars for t in wins)
    gross_loss = abs(sum(t.pnl_dollars for t in losses))
    total_pnl = sum(t.pnl_dollars for t in trades)

    # Simple equity curve for max drawdown
    equity = [initial_equity]
    running = initial_equity
    for t in sorted(trades, key=lambda x: x.exit_date):
        running += t.pnl_dollars
        equity.append(running)
    peak = np.maximum.accumulate(equity) if equity else [initial_equity]
    dd = [(peak[i] - equity[i]) / peak[i] * 100 for i in range(len(equity))]
    max_dd = max(dd) if dd else 0

    pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0)

    # Raw single-test significance. This is NOT yet corrected for how many
    # configurations were tried — `random_search` does that across the panel.
    pnl_pcts = [t.pnl_pct for t in trades]
    t_stat, p_value = t_statistic(pnl_pcts)

    return dict(
        trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate=round(len(wins) / len(trades) * 100, 1),
        total_pnl=round(total_pnl, 2),
        profit_factor=pf,
        avg_pnl_pct=round(np.mean(pnl_pcts) * 100, 2),
        max_drawdown=round(max_dd, 2),
        sharpe=round(sharpe_ratio(pnl_pcts), 3),
        t_stat=round(t_stat, 3),
        p_value=round(p_value, 5),
    )


# ── Parameter grid search ────────────────────────────────────────────────────

# Deliberately bounded search spaces. Every field controls the selected strategy.
# F16's ineffective/ambiguous rule fields are excluded until separately resolved.
PARAMETER_SPACES = {
    'trend_pullback': {'stop_loss_pct': (.05, .15), 'atr_tp_multiple': (1.5, 4.),
                       'rsi_pullback_max': (40, 65), 'rsi_lookback': (5, 20)},
    'breakout': {'breakout_stop_loss_pct': (.05, .15), 'breakout_atr_multiple': (1.5, 4.)},
    'mean_reversion': {'mr_stop_loss_pct': (.03, .12), 'mr_atr_multiple': (1., 3.)},
    'momentum_macd': {'macd_stop_loss_pct': (.05, .15), 'macd_tp_multiple': (1.5, 4.)},
    'ensemble': {'ensemble_stop_loss_pct': (.05, .15), 'ensemble_tp_multiple': (1.5, 4.)},
    'regime': {'stop_loss_pct': (.05, .15), 'atr_tp_multiple': (1.5, 4.)},
    'sma_50_cross': {'sma_cross_stop_loss_pct': (.05, .15)},
    'tqqq_momentum': {'tqqq_stop_loss_pct': (.04, .12)},
}


def random_search(
    strategy: StrategyType,
    year: int,
    iterations: int = 30,
    seed: int = 42,
    alpha: float = 0.05,
    n_boot: int = 1000,
    *, baseline: StrategyParams = PARAMS,
) -> tuple[list[dict], SignificanceReport]:
    """Random search over strategy params, priced against the search itself.

    Returns (results sorted best-to-worst by total_pnl, significance report for
    the winner).

    The report is the point of this function, not the ranking. Sorting N
    configurations by P&L and taking the top one is a maximum over N correlated
    tests; the winner's raw t-statistic is therefore not comparable to a t > 2
    bar. `evaluate_search` calibrates the real hurdle by bootstrapping the
    distribution of that maximum under the null. Expect most sweeps to fail it —
    that is the correct outcome, not a bug.
    """
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 1:
        raise ValueError('iterations must be a positive integer')
    rng = random.Random(seed)
    results, evaluated = [], {}
    param_bounds = PARAMETER_SPACES[strategy.value]
    dataset = load_dataset(strategy, year)

    for i in range(iterations):
        overrides = {}
        for name, (lo, hi) in param_bounds.items():
            if isinstance(lo, int) and isinstance(hi, int):
                overrides[name] = rng.randint(lo, hi)
            else:
                overrides[name] = round(rng.uniform(lo, hi), 2)

        new_p = replace(baseline, strategy=strategy, **overrides)
        config_id = hashlib.sha256(json.dumps(asdict(new_p), sort_keys=True, default=str).encode()).hexdigest()
        duplicate_of = evaluated[config_id]['label'] if config_id in evaluated else None
        if duplicate_of is None:
            trades = run_backtest_for_params(new_p, strategy, year, dataset=dataset)
            stats = compute_stats(trades, new_p.initial_backtest_equity)
            returns = returns_from_trades(trades)
        else:
            stats = deepcopy(evaluated[config_id]['stats'])
            returns = deepcopy(evaluated[config_id]['returns'])

        label = f"cfg{i:03d}"
        results.append({
            "label": label,
            "overrides": overrides,
            "stats": stats,
            "returns": returns,
            "params": asdict(new_p),
            "config_id": config_id,
            "duplicate_of": duplicate_of,
            "dataset": dataset.metadata,
        })
        evaluated.setdefault(config_id, results[-1])
        log.info("  [%d/%d] P&L=$%.2f WR=%.1f%% trades=%d t=%.2f params=%s",
                 i + 1, iterations, stats["total_pnl"], stats["win_rate"],
                 stats["trades"], stats["t_stat"], overrides)

    search = dict(attempted_trials=iterations, distinct_configurations=len(evaluated),
                  duplicate_trials=iterations-len(evaluated))
    for result in results:
        result['search'] = dict(search)
    log.info('Search accounting: %d attempts, %d distinct configurations, %d repeats; dataset=%s',
             iterations, len(evaluated), iterations-len(evaluated), dataset.metadata['fingerprint'])

    results.sort(key=lambda r: r["stats"]["total_pnl"], reverse=True)

    # Price the winner against every variant tried — including the losers, whose
    # omission would inflate the result as badly as no correction at all.
    configs = {r["label"]: r["returns"] for r in results}
    winner = results[0]["label"] if results else None
    report, bhy_adjusted = evaluate_search(
        configs, winner=winner, alpha=alpha, n_boot=n_boot, seed=seed
    )
    for r in results:
        r["stats"]["bhy_p_value"] = round(bhy_adjusted.get(r["label"], 1.0), 5)

    log.info("Multiple-testing verdict for best of %d configurations:\n%s",
             iterations, report.summary())
    return results, report
