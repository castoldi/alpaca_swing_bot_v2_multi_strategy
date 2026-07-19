"""Full-year 2026 backtest — all registered strategies on real market data.

Usage:
    python backtest_2026.py                         # all strategies
    python backtest_2026.py --strategy breakout
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from config import PARAMS, TICKERS, BAR_TIMEFRAME
from logger_setup import get_logger
from strategies import REGISTRY, get_enabled
from dashboard import db as db_mod

log = get_logger(__name__)
ROOT = Path(__file__).parent
REPORTS_DIR = ROOT / "reports"
OUTPUT_PATH = REPORTS_DIR / "backtest_2026.html"

BACKTEST_START = date(2026, 1, 1)
BACKTEST_END = date(2026, 12, 31)

# Import shared modules from 2025 backtest
from backtest_2025 import (
    download_history, run_strategy_year,
    TICKERS as TKS,  # noqa: F811
)
from build_report_2025 import (
    STRATEGY_COLORS, build_report_2025,
)


def run_full_backtest() -> int:
    log.info("=== 2026 Multi-Strategy Backtest (V2) ===")
    strategies_to_run = get_enabled()
    timeframes = sorted({strategy.timeframe for strategy in strategies_to_run})
    log.info("Downloading %d tickers for timeframes %s...", len(TICKERS), timeframes)

    ticker_data: dict[tuple[str, str], pd.DataFrame] = {}
    for timeframe in timeframes:
        for tk in TICKERS:
            log.info("Downloading %s (%s)...", tk, timeframe)
            df = download_history(tk, BACKTEST_START, BACKTEST_END, timeframe)
            if df.empty or len(df) < PARAMS.sma_slow + 5:
                log.warning("%s (%s): insufficient data — skipping", tk, timeframe)
                df = pd.DataFrame()
            ticker_data[(tk, timeframe)] = df

    strategy_results = {}
    per_strategy_details = {}
    overall_best = None
    best_pnl = float("-inf")

    for strategy in strategies_to_run:
        strat_name = strategy.name
        log.info("=" * 50)
        log.info("Running strategy: %s", strat_name)
        log.info("=" * 50)

        bt_id = db_mod.start_backtest_run(2026, strat_name, strategy.timeframe)
        _, stats, per_ticker = run_strategy_year(
            ticker_data, strategy, 2026, PARAMS
        )
        dd = stats["max_drawdown_pct"]

        strategy_results[strat_name] = stats
        per_strategy_details[strat_name] = per_ticker

        pf = stats["profit_factor"]
        db_mod.finish_backtest_run(bt_id, stats["trades"], stats["win_rate"],
                                    stats["total_pnl"], pf if pf != float("inf") else 999.0,
                                    dd, 0.0)

        if stats["total_pnl"] > best_pnl:
            best_pnl = stats["total_pnl"]
            overall_best = strat_name

        log.info("  %s: %d trades, WR=%.1f%%, P&L=%+.2f, PF=%.2f, DD=%.1f%%%s",
                 strat_name, stats["trades"], stats["win_rate"] * 100,
                 stats["total_pnl"], stats["profit_factor"], dd * 100,
                 " (best!)" if overall_best == strat_name else "")

    # Build report using shared builder
    html = build_report_2025(strategy_results, per_strategy_details, overall_best)
    # Replace title for 2026
    html = html.replace("2025 Backtest", "2026 Backtest")
    OUTPUT_PATH.write_text(html, encoding="utf-8")

    log.info("=" * 50)
    log.info("2026 backtest complete. Best strategy: %s ($%.2f)", overall_best, best_pnl)
    log.info("Report: %s", OUTPUT_PATH)
    return 0


def run_single(strategy_name: str) -> int:
    if strategy_name not in REGISTRY:
        log.error("Unknown strategy '%s'. Available: %s", strategy_name, list(REGISTRY.keys()))
        return 1
    strat = REGISTRY[strategy_name]
    log.info("Single-strategy backtest: %s (2026)", strat.name)
    ticker_data: dict[str, pd.DataFrame] = {}
    for tk in TICKERS:
        df = download_history(tk, BACKTEST_START, BACKTEST_END, strat.timeframe)
        ticker_data[tk] = df if not df.empty and len(df) >= PARAMS.sma_slow + 5 else pd.DataFrame()
    bt_id = db_mod.start_backtest_run(2026, strat.name, strat.timeframe)
    _, stats, _ = run_strategy_year(ticker_data, strat, 2026, PARAMS)
    dd = stats["max_drawdown_pct"]
    pf = stats["profit_factor"]
    db_mod.finish_backtest_run(bt_id, stats["trades"], stats["win_rate"],
                               stats["total_pnl"], pf if pf != float("inf") else 999.0, dd, 0.0)
    log.info("%s: %d trades, WR=%.1f%%, P&L=%+.2f",
             strat.name, stats["trades"], stats["win_rate"] * 100, stats["total_pnl"])
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="2026 multi-strategy backtest")
    parser.add_argument("--strategy", type=str, help="Run a single strategy by name")
    args = parser.parse_args()
    if args.strategy:
        sys.exit(run_single(args.strategy))
    sys.exit(run_full_backtest())
