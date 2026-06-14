"""Full-year 2025 backtest — all 6 strategies on real market data.

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

from config import PARAMS, TICKERS, StrategyType, BAR_TIMEFRAME, HISTORY_WARMUP_DAYS
from logger_setup import get_logger
from strategy import Trade, add_indicators, backtest_ticker
from dashboard import db as db_mod
import data_feed

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
])


def download_history(ticker: str, start: date, end: date) -> pd.DataFrame:
    """Fetch 4h bars from Alpaca with a warmup window so indicators are primed.

    (Previously yfinance daily bars — switched to Alpaca 4h; see data_feed.py.)
    """
    warmup_start = start - timedelta(days=HISTORY_WARMUP_DAYS)
    return data_feed.fetch_4h(ticker, warmup_start, end + timedelta(days=1))


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
                    total_time_pnl=0.0, tp_count=0, sl_count=0, time_count=0,
                    avg_win_pct=0.0, avg_loss_pct=0.0, total_volume=0.0, profit_factor=0.0)
    tp_t = [t for t in trades if t.exit_reason == "take_profit"]
    sl_t = [t for t in trades if t.exit_reason == "stop_loss"]
    time_t = [t for t in trades if t.exit_reason == "time_stop"]
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
        tp_count=len(tp_t), sl_count=len(sl_t), time_count=len(time_t),
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
    log.info("Downloading history for %d tickers from yfinance...", len(TICKERS))

    ticker_data: dict[str, pd.DataFrame] = {}
    for tk in TICKERS:
        log.info("Downloading %s...", tk)
        df = download_history(tk, BACKTEST_START, BACKTEST_END)
        if df.empty or len(df) < PARAMS.sma_slow + 5:
            log.warning("%s: insufficient data — skipping", tk)
            ticker_data[tk] = pd.DataFrame()
            continue
        ticker_data[tk] = df

    # V2 strategies to test
    v2_strategies = [
        StrategyType.TREND_PULLBACK,
        StrategyType.BREAKOUT,
        StrategyType.MEAN_REVERSION,
        StrategyType.MOMENTUM_MACD,
        StrategyType.ENSEMBLE,
        StrategyType.REGIME_ADAPTIVE,
    ]

    strategy_results = {}
    per_strategy_details = {}
    overall_best = None
    best_pnl = float("-inf")

    for strategy in v2_strategies:
        strat_name = strategy.value
        log.info("=" * 50)
        log.info("Running strategy: %s", strat_name)
        log.info("=" * 50)

        per_ticker: dict[str, tuple[pd.DataFrame, list[Trade]]] = {}
        all_candidate_trades: list[Trade] = []

        # Record backtest run in DB
        bt_id = db_mod.start_backtest_run(2025, strat_name)

        for tk in TICKERS:
            df = ticker_data.get(tk)
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="2025 multi-strategy backtest")
    parser.add_argument("--strategy", type=str, help="Run single strategy only")
    args = parser.parse_args()
    if args.strategy:
        # Single strategy mode
        import importlib
        mod = importlib.import_module(__name__)
        sys.exit(mod.run_single(args.strategy))
    sys.exit(run_full_backtest())