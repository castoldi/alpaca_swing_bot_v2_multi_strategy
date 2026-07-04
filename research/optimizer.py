"""Research optimizer — parameter tuning via grid/random search for strategy improvement."""
from __future__ import annotations

import itertools
import random
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
import yfinance as yf

from config import PARAMS, TICKERS, StrategyParams, StrategyType
from logger_setup import get_logger
from strategy import add_indicators, backtest_ticker, Trade

log = get_logger(__name__)
ROOT = Path(__file__).parent.parent
REPORTS_DIR = ROOT / "reports"


# ── Data helper ──────────────────────────────────────────────────────────────

def download_history(ticker: str, start: date, end: date) -> pd.DataFrame:
    warmup_start = date(start.year - 1, start.month, start.day)
    raw = yf.download(
        ticker,
        start=warmup_start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        interval="1d",
        auto_adjust=True,
        progress=False,
    )
    if raw.empty:
        return raw
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df


from datetime import timedelta


def apply_portfolio_cap(trades: list[Trade], dollars_per_trade: float, cap: float) -> list[Trade]:
    max_concurrent = int(cap // dollars_per_trade) if dollars_per_trade else 0
    accepted: list[Trade] = []
    open_exit_dates: list[pd.Timestamp] = []
    for t in sorted(trades, key=lambda x: x.entry_date):
        open_exit_dates = [d for d in open_exit_dates if d >= t.entry_date]
        if len(open_exit_dates) >= max_concurrent:
            continue
        accepted.append(t)
        open_exit_dates.append(t.exit_date)
    return accepted


def run_backtest_for_params(
    p: StrategyParams,
    strategy: StrategyType,
    year: int,
) -> list[Trade]:
    """Run a full multi-ticker backtest for a given year with custom params."""
    start = date(year, 1, 1)
    end = date(year, 12, 31)
    all_trades: list[Trade] = []

    for ticker in TICKERS:
        df = download_history(ticker, start, end)
        if df.empty or len(df) < 60:
            continue
        window_start = pd.Timestamp(start)
        trades = backtest_ticker(df, ticker, window_start, p, strategy)
        all_trades.extend(trades)

    capped = apply_portfolio_cap(all_trades, p.dollars_per_trade, p.max_concurrent_capital)
    return capped


def compute_stats(trades: list[Trade]) -> dict[str, Any]:
    if not trades:
        return dict(trades=0, wins=0, losses=0, win_rate=0, total_pnl=0, profit_factor=0, avg_pnl_pct=0, max_drawdown=0)

    wins = [t for t in trades if t.pnl_dollars > 0]
    losses = [t for t in trades if t.pnl_dollars <= 0]
    gross_profit = sum(t.pnl_dollars for t in wins)
    gross_loss = abs(sum(t.pnl_dollars for t in losses))
    total_pnl = sum(t.pnl_dollars for t in trades)

    # Simple equity curve for max drawdown
    equity = []
    running = 0.0
    for t in sorted(trades, key=lambda x: x.entry_date):
        running += t.pnl_dollars
        equity.append(running)
    peak = np.maximum.accumulate(equity) if equity else [0]
    dd = [(peak[i] - equity[i]) / peak[i] * 100 if peak[i] > 0 else 0 for i in range(len(equity))]
    max_dd = max(dd) if dd else 0

    pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0)

    return dict(
        trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        win_rate=round(len(wins) / len(trades) * 100, 1),
        total_pnl=round(total_pnl, 2),
        profit_factor=pf,
        avg_pnl_pct=round(np.mean([t.pnl_pct for t in trades]) * 100, 2),
        max_drawdown=round(max_dd, 2),
    )


# ── Parameter grid search ────────────────────────────────────────────────────

def _mutate_param(name: str, current: float, bounds: tuple[float, float], step: float) -> float:
    """Random walk mutation within bounds."""
    delta = random.choice([-step, -step * 0.5, step * 0.5, step])
    new_val = current + delta
    return round(max(bounds[0], min(bounds[1], new_val)), 4)


def random_search(
    strategy: StrategyType,
    year: int,
    iterations: int = 30,
    seed: int = 42,
) -> list[dict]:
    """Random search over strategy params to find optimal settings.

    Returns sorted list of (stats, param_overrides) from best to worst total_pnl.
    """
    random.seed(seed)
    np.random.seed(seed)
    results = []

    # Param search space
    param_bounds = {
        "dollars_per_trade": (100, 500),
        "stop_loss_pct": (0.05, 0.15),
        "atr_tp_multiple": (1.5, 4.0),
        "rsi_pullback_max": (40, 65),
        "rsi_lookback": (5, 20),
    }

    for i in range(iterations):
        overrides = {}
        for name, (lo, hi) in param_bounds.items():
            if isinstance(lo, int) and isinstance(hi, int):
                overrides[name] = random.randint(lo, hi)
            else:
                overrides[name] = round(random.uniform(lo, hi), 2)

        # Build custom params
        p = PARAMS
        new_p = StrategyParams(
            strategy=strategy,
            dollars_per_trade=overrides.get("dollars_per_trade", p.dollars_per_trade),
            stop_loss_pct=overrides.get("stop_loss_pct", p.stop_loss_pct),
            atr_tp_multiple=overrides.get("atr_tp_multiple", p.atr_tp_multiple),
            rsi_pullback_max=overrides.get("rsi_pullback_max", p.rsi_pullback_max),
            rsi_lookback=overrides.get("rsi_lookback", p.rsi_lookback),
        )

        trades = run_backtest_for_params(new_p, strategy, year)
        stats = compute_stats(trades)

        results.append({"overrides": overrides, "stats": stats})
        log.info("  [%d/%d] P&L=$%.2f WR=%.1f%% trades=%d params=%s",
                 i + 1, iterations, stats["total_pnl"], stats["win_rate"],
                 stats["trades"], overrides)

    results.sort(key=lambda r: r["stats"]["total_pnl"], reverse=True)
    return results