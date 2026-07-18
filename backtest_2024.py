"""Full-year 2024 backtest — all registered strategies on real market data.

Usage:
    python backtest_2024.py                         # all strategies
    python backtest_2024.py --strategy breakout
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

from config import PARAMS, TICKERS, BAR_TIMEFRAME
from logger_setup import get_logger
from strategies import REGISTRY, get_enabled, Trade, backtest_ticker
from dashboard import db as db_mod
from backtest_2025 import download_history, apply_portfolio_cap, compute_stats, compute_max_drawdown
from build_report_2025 import build_report_2025

log = get_logger(__name__)
ROOT = Path(__file__).parent
OUTPUT_PATH = ROOT / "reports" / "backtest_2024.html"

BACKTEST_START = date(2024, 1, 1)
BACKTEST_END = date(2024, 12, 31)


def run_full_backtest() -> int:
    log.info("=== 2024 Multi-Strategy Backtest (V2) ===")
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

        per_ticker: dict[str, tuple[pd.DataFrame, list[Trade]]] = {}
        all_candidate_trades: list[Trade] = []

        bt_id = db_mod.start_backtest_run(2024, strat_name, strategy.timeframe)

        for tk in TICKERS:
            df = ticker_data.get((tk, strategy.timeframe))
            if df is None or df.empty:
                continue
            window_start = pd.Timestamp(BACKTEST_START)
            trades = backtest_ticker(df, tk, window_start, PARAMS, strategy)
            all_candidate_trades.extend(trades)
            per_ticker[tk] = (df, trades)

        capped, skipped = apply_portfolio_cap(
            all_candidate_trades, PARAMS.dollars_per_trade, PARAMS.max_concurrent_capital
        )
        stats = compute_stats(capped)
        dd = compute_max_drawdown(capped)
        stats["max_drawdown_pct"] = dd
        stats["roi_on_cap"] = stats["total_pnl"] / PARAMS.max_concurrent_capital
        stats["_trades"] = capped
        stats["skipped"] = skipped

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

    html = build_report_2025(strategy_results, per_strategy_details, overall_best)
    html = html.replace("2025 Backtest", "2024 Backtest")
    OUTPUT_PATH.write_text(html, encoding="utf-8")

    log.info("=" * 50)
    log.info("2024 backtest complete. Best strategy: %s ($%.2f)", overall_best, best_pnl)
    log.info("Report: %s", OUTPUT_PATH)
    return 0


def run_single(strategy_name: str) -> int:
    if strategy_name not in REGISTRY:
        log.error("Unknown strategy '%s'. Available: %s", strategy_name, list(REGISTRY.keys()))
        return 1
    strat = REGISTRY[strategy_name]
    log.info("Single-strategy backtest: %s (2024)", strat.name)
    ticker_data: dict[str, pd.DataFrame] = {}
    for tk in TICKERS:
        df = download_history(tk, BACKTEST_START, BACKTEST_END, strat.timeframe)
        ticker_data[tk] = df if not df.empty and len(df) >= PARAMS.sma_slow + 5 else pd.DataFrame()
    bt_id = db_mod.start_backtest_run(2024, strat.name, strat.timeframe)
    all_trades: list[Trade] = []
    per_ticker: dict = {}
    for tk in TICKERS:
        df = ticker_data.get(tk)
        if df is None or df.empty:
            continue
        trades = backtest_ticker(df, tk, pd.Timestamp(BACKTEST_START), PARAMS, strat)
        all_trades.extend(trades)
        per_ticker[tk] = (df, trades)
    capped, _ = apply_portfolio_cap(all_trades, PARAMS.dollars_per_trade, PARAMS.max_concurrent_capital)
    stats = compute_stats(capped)
    dd = compute_max_drawdown(capped)
    pf = stats["profit_factor"]
    db_mod.finish_backtest_run(bt_id, stats["trades"], stats["win_rate"],
                               stats["total_pnl"], pf if pf != float("inf") else 999.0, dd, 0.0)
    log.info("%s: %d trades, WR=%.1f%%, P&L=%+.2f",
             strat.name, stats["trades"], stats["win_rate"] * 100, stats["total_pnl"])
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="2024 multi-strategy backtest")
    parser.add_argument("--strategy", type=str, help="Run a single strategy by name")
    args = parser.parse_args()
    if args.strategy:
        sys.exit(run_single(args.strategy))
    sys.exit(run_full_backtest())
