"""Full-year 2025 backtest — all registered strategies on real market data.

Downloads ~2 years of history from yfinance, runs every strategy on every
trading day of 2025, applies per-strategy portfolio caps, and writes a
comprehensive Plotly HTML report with strategy comparison.

Usage:
    python backtest_2025.py                         # all strategies
    python backtest_2025.py --strategy breakout
    python backtest_2025.py --strategy momentum_macd
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from config import PARAMS, TICKERS, BAR_TIMEFRAME, HISTORY_WARMUP_DAYS
from logger_setup import get_logger
from strategies import REGISTRY, get_enabled, Trade, add_indicators, backtest_ticker
from dashboard import db as db_mod
import data_feed
from market_cache import MarketDataCache

log = get_logger(__name__)
ROOT = Path(__file__).parent
REPORTS_DIR = ROOT / "reports"
OUTPUT_PATH = REPORTS_DIR / "backtest_2025.html"

BACKTEST_START = date(2025, 1, 1)
BACKTEST_END = date(2025, 12, 31)

# ── Colours (same as V1 for consistency) ──────────────────────────────────────
PROFIT_COLOR = "#22c55e"; LOSS_COLOR = "#ef4444"; NEUTRAL_COLOR = "#6b7280"
PRICE_COLOR = "#60a5fa"; BG_DARK = "#0f1117"; BG_CARD = "#1a1d27"
BORDER_COLOR = "#2a2d35"; TEXT_PRIMARY = "#e1e7ef"; TEXT_SECONDARY = "#8892a4"
TEXT_MUTED = "#6b7280"
from collections import OrderedDict
STRATEGY_COLORS = OrderedDict([
    ("trend_pullback", "#60a5fa"), ("breakout", "#f59e0b"),
    ("mean_reversion", "#a78bfa"), ("momentum_macd", "#34d399"),
    ("ensemble", "#f472b6"), ("regime", "#fb923c"),
    ("sma_50_cross", "#38bdf8"),
])

_MARKET_CACHE = MarketDataCache()


def download_history(
    ticker: str, start: date, end: date, timeframe: str = BAR_TIMEFRAME
) -> pd.DataFrame:
    """Fetch strategy-timeframe bars with a warmup window for indicators."""
    warmup_start = start - timedelta(days=HISTORY_WARMUP_DAYS)
    bars = _MARKET_CACHE.get_bars(
        ticker,
        warmup_start,
        end + timedelta(days=1),
        timeframe,
        feed="sip",
    )
    return data_feed.completed_bars(bars, timeframe)


def apply_portfolio_cap(trades: list[Trade], dollars_per_trade: float, cap: float) -> tuple[list[Trade], int]:
    max_concurrent = int(cap // dollars_per_trade) if dollars_per_trade else 0
    accepted, skipped = [], 0
    open_exit_dates = []
    for t in sorted(trades, key=lambda x: x.entry_date):
        open_exit_dates = [d for d in open_exit_dates if d >= t.entry_date]
        if len(open_exit_dates) >= max_concurrent:
            skipped += 1
            continue
        accepted.append(t)
        open_exit_dates.append(t.exit_date)
    return accepted, skipped


def compute_stats(trades: list[Trade]) -> dict:
    if not trades:
        return dict(trades=0, wins=0, losses=0, win_rate=0.0, total_pnl=0.0,
                    avg_pnl_pct=0.0, best_pct=0.0, worst_pct=0.0,
                    avg_bars_held=0.0, total_tp_pnl=0.0, total_sl_pnl=0.0,
                    total_time_pnl=0.0, total_signal_pnl=0.0,
                    tp_count=0, sl_count=0, time_count=0, signal_count=0,
                    avg_win_pct=0.0, avg_loss_pct=0.0, total_volume=0.0, profit_factor=0.0)
    TP_REASONS = {"take_profit", "tp1", "tp2", "tp3"}
    tp_t = [t for t in trades if t.exit_reason in TP_REASONS]
    sl_t = [t for t in trades if t.exit_reason in {"stop_loss", "gap_stop"}]
    time_t = [t for t in trades if t.exit_reason == "time_stop"]
    signal_t = [t for t in trades if t.exit_reason == "sma_cross_down"]
    win_pnls = [t.pnl_dollars for t in trades if t.pnl_dollars > 0]
    loss_pnls = [t.pnl_dollars for t in trades if t.pnl_dollars <= 0]
    total_gross_profit = sum(win_pnls) or 0.0
    total_gross_loss = abs(sum(loss_pnls)) or 0.0
    profit_factor = total_gross_profit / total_gross_loss if total_gross_loss > 0 else float("inf")
    return dict(
        trades=len(trades), wins=len(win_pnls), losses=len(loss_pnls),
        win_rate=len(win_pnls) / len(trades) if trades else 0.0,
        total_pnl=sum(t.pnl_dollars for t in trades),
        avg_pnl_pct=float(np.mean([t.pnl_pct for t in trades])),
        best_pct=float(np.max([t.pnl_pct for t in trades])),
        worst_pct=float(np.min([t.pnl_pct for t in trades])),
        avg_bars_held=float(np.mean([t.bars_held for t in trades])),
        total_tp_pnl=sum(t.pnl_dollars for t in tp_t),
        total_sl_pnl=sum(t.pnl_dollars for t in sl_t),
        total_time_pnl=sum(t.pnl_dollars for t in time_t),
        total_signal_pnl=sum(t.pnl_dollars for t in signal_t),
        tp_count=len(tp_t), sl_count=len(sl_t), time_count=len(time_t),
        signal_count=len(signal_t),
        avg_win_pct=float(np.mean([t.pnl_pct for t in trades if t.pnl_dollars > 0])) * 100 if win_pnls else 0.0,
        avg_loss_pct=float(np.mean([t.pnl_pct for t in trades if t.pnl_dollars <= 0])) * 100 if loss_pnls else 0.0,
        total_volume=sum(abs(t.pnl_dollars) for t in trades),
        profit_factor=profit_factor,
    )


def per_ticker_stats(ticker: str, trades: list[Trade]) -> dict:
    s = compute_stats(trades)
    s["ticker"] = ticker
    return s

def compute_max_drawdown(trades: list[Trade]) -> float:
    """Return maximum drawdown % from peak equity. Uses total capital at risk as denominator."""
    if not trades:
        return 0.0
    sorted_t = sorted(trades, key=lambda t: t.exit_date)
    cum = np.cumsum([t.pnl_dollars for t in sorted_t])
    running_max = np.maximum.accumulate(cum)
    dd = cum - running_max
    # Use max capital deployed as denominator instead of running_max (which can be 0)
    denominator = PARAMS.max_concurrent_capital
    if denominator <= 0:
        return 0.0
    dd_pct = dd / denominator
    return float(abs(dd_pct.min())) if len(dd_pct) > 0 else 0.0


def run_full_backtest() -> int:
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

        # Record backtest run in DB
        bt_id = db_mod.start_backtest_run(2025, strat_name, strategy.timeframe)

        for tk in TICKERS:
            df = ticker_data.get((tk, strategy.timeframe))
            if df is None or df.empty:
                continue
            window_start = pd.Timestamp(BACKTEST_START)
            trades = backtest_ticker(df, tk, window_start, PARAMS, strategy)
            all_candidate_trades.extend(trades)
            per_ticker[tk] = (df, trades)

        capped, skipped = apply_portfolio_cap(all_candidate_trades,
                                               PARAMS.dollars_per_trade,
                                               PARAMS.max_concurrent_capital)
        stats = compute_stats(capped)
        dd = compute_max_drawdown(capped)
        stats["max_drawdown_pct"] = dd
        stats["roi_on_cap"] = stats["total_pnl"] / PARAMS.max_concurrent_capital
        stats["_trades"] = capped
        stats["skipped"] = skipped

        strategy_results[strat_name] = stats
        per_strategy_details[strat_name] = per_ticker

        # Log to DB
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
                 " (best so far!)" if overall_best == strat_name else "")

    # ── Build HTML report ──────────────────────────────────────────────────────
    from build_report_2025 import build_report_2025
    html = build_report_2025(strategy_results, per_strategy_details, overall_best)
    OUTPUT_PATH.write_text(html, encoding="utf-8")

    log.info("=" * 50)
    log.info("2025 backtest complete. Best strategy: %s ($%.2f)", overall_best, best_pnl)
    log.info("Report: %s", OUTPUT_PATH)
    return 0


def run_single(strategy_name: str) -> int:
    if strategy_name not in REGISTRY:
        log.error("Unknown strategy '%s'. Available: %s", strategy_name, list(REGISTRY.keys()))
        return 1
    strat = REGISTRY[strategy_name]
    strat_name = strat.name
    log.info("Single-strategy backtest: %s (2025)", strat_name)

    ticker_data: dict[str, pd.DataFrame] = {}
    for tk in TICKERS:
        df = download_history(tk, BACKTEST_START, BACKTEST_END, strat.timeframe)
        if df.empty or len(df) < PARAMS.sma_slow + 5:
            ticker_data[tk] = pd.DataFrame()
        else:
            ticker_data[tk] = df

    bt_id = db_mod.start_backtest_run(2025, strat_name, strat.timeframe)
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
             strat_name, stats["trades"], stats["win_rate"] * 100, stats["total_pnl"])
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="2025 multi-strategy backtest")
    parser.add_argument("--strategy", type=str, help="Run a single strategy by name")
    args = parser.parse_args()
    if args.strategy:
        sys.exit(run_single(args.strategy))
    sys.exit(run_full_backtest())
