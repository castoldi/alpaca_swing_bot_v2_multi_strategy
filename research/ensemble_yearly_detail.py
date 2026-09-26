"""Year-by-year detail for one strategy at one account size (default ensemble, $5,000).

Two engines, reported separately because they are not equally trustworthy:

  realistic  2022+  production path: one-minute execution, earnings filter,
             daily-loss kill switch (IEX minute bars start 2022). Reuses the
             candidate cache written by research/capital_size_experiment.py.
  simple     2016+  Alpaca SIP 4h candles, next-bar-open fills, no kill switch
             (no minute data before 2022). Also run on 2022+ so the gap between
             the two engines is visible.

Benchmark: equal-weight buy-and-hold of the tickers listed that year, first to
last 4h close. Drawdown is realized-equity (open-position dips are not seen).

Usage:  python research/ensemble_yearly_detail.py [--strategy ensemble] [--capital 5000]
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import replace
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_feed  # noqa: E402
from backtest_2025 import compute_max_drawdown  # noqa: E402
from backtest_portfolio import (  # noqa: E402
    collect_backtest_candidates, run_annual_portfolio, run_configured_portfolio,
)
from config import PARAMS, TICKERS  # noqa: E402
from market_cache import MarketDataCache  # noqa: E402
from research import capital_size_experiment as cx  # noqa: E402
from strategies import REGISTRY, strategy_universe  # noqa: E402

REALISTIC_YEARS = range(2022, 2027)
SIMPLE_YEARS = range(2016, 2027)
SIP_END = pd.Timestamp("2026-09-10")


def window(year):
    start = pd.Timestamp(date(year, 1, 1))
    return start, start + pd.DateOffset(years=1) - pd.Timedelta(nanoseconds=1)


def benchmark(frames: dict[str, pd.DataFrame], year: int) -> float:
    start, end = window(year)
    rets = []
    for f in frames.values():
        closes = f.loc[(f.index >= start) & (f.index <= end), "close"]
        if len(closes) > 1:
            rets.append(closes.iloc[-1] / closes.iloc[0] - 1)
    return float(np.mean(rets)) if rets else float("nan")


def detail(year: int, result, capital: float, bench: float) -> dict:
    trades = list(result.trades)
    pnls = np.array([t.pnl_dollars for t in trades]) if trades else np.array([])
    pcts = np.array([t.pnl_pct for t in trades]) if trades else np.array([])
    wins, losses = pnls[pnls > 0], pnls[pnls <= 0]
    reasons = Counter(
        "target" if t.exit_reason in {"take_profit", "tp1", "tp2", "tp3"}
        else "stop" if t.exit_reason in {"stop_loss", "gap_stop"}
        else "time" if "time" in t.exit_reason else t.exit_reason
        for t in trades)
    by_ticker = {}
    for t in trades:
        by_ticker[t.ticker] = by_ticker.get(t.ticker, 0.0) + t.pnl_dollars
    best = max(trades, key=lambda t: t.pnl_pct) if trades else None
    worst = min(trades, key=lambda t: t.pnl_pct) if trades else None
    monthly = {}
    for t in trades:
        m = pd.Timestamp(t.exit_date).month
        monthly[m] = monthly.get(m, 0.0) + t.pnl_dollars
    return dict(
        year=year, ret=result.return_pct, pnl=result.ending_equity - capital,
        end=result.ending_equity, bench=bench, n=len(trades),
        win=len(wins) / len(trades) if trades else 0.0,
        avg_win=float(pcts[pcts > 0].mean()) if (pcts > 0).any() else 0.0,
        avg_loss=float(pcts[pcts <= 0].mean()) if (pcts <= 0).any() else 0.0,
        pf=float(wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf"),
        dd=compute_max_drawdown(trades, capital), skipped=result.skipped_positions,
        kill=getattr(result, "kill_switch_trip_days", 0),
        best=f"{best.ticker} {best.pnl_pct * 100:+.1f}%" if best else "-",
        worst=f"{worst.ticker} {worst.pnl_pct * 100:+.1f}%" if worst else "-",
        exits=" ".join(f"{k}:{v}" for k, v in reasons.most_common()),
        tickers=" ".join(f"{k} {v:+,.0f}" for k, v in sorted(by_ticker.items(), key=lambda kv: -kv[1])),
        worst_month=(min(monthly.items(), key=lambda kv: kv[1]) if monthly else (0, 0.0)),
    )


def print_table(title: str, rows: list[dict], capital: float) -> None:
    print(f"\n=== {title} ===")
    print(f"{'year':>5} {'return':>8} {'P&L $':>9} {'end $':>9} {'B&H':>7} {'trades':>6} {'win%':>5} "
          f"{'avgW':>6} {'avgL':>6} {'PF':>5} {'maxDD':>6} {'skip':>5} {'KS':>3}  best / worst trade")
    comp = 1.0
    for r in rows:
        comp *= 1 + r["ret"]
        pf = "inf" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
        print(f"{r['year']:>5} {r['ret'] * 100:>+7.1f}% {r['pnl']:>+9,.0f} {r['end']:>9,.0f} "
              f"{r['bench'] * 100:>+6.0f}% {r['n']:>6} {r['win'] * 100:>4.0f}% {r['avg_win'] * 100:>+5.1f}% "
              f"{r['avg_loss'] * 100:>+5.1f}% {pf:>5} {r['dd'] * 100:>5.1f}% {r['skipped']:>5} {r['kill']:>3}  "
              f"{r['best']} / {r['worst']}")
    print(f"compounded {(comp - 1) * 100:+.1f}% over {len(rows)} years "
          f"(each year restarts at ${capital:,.0f}; compounding assumes profits are kept in)")
    for r in rows:
        m, v = r["worst_month"]
        print(f"  {r['year']}: exits {r['exits']} | by ticker {r['tickers']} | worst month "
              f"{pd.Timestamp(2000, m or 1, 1):%b} {v:+,.0f}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strategy", default="ensemble")
    parser.add_argument("--capital", type=float, default=5000)
    args = parser.parse_args()
    strategy = REGISTRY[args.strategy]
    params = replace(PARAMS, initial_backtest_equity=args.capital)
    cache_dir = ROOT / "cache" / "capital_experiment"
    cache_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for year in REALISTIC_YEARS:
        frames = cx.active_frames(args.strategy, year)
        result = run_configured_portfolio(cx.candidates(args.strategy, year, cache_dir),
                                          params=params, price_frames=frames)
        rows.append(detail(year, result, args.capital, benchmark(frames, year)))
    print_table(f"{args.strategy} @ ${args.capital:,.0f} — realistic engine (minute fills), 2022–2026",
                rows, args.capital)

    cache = MarketDataCache()
    sip = {}
    for t in strategy_universe(strategy, TICKERS):
        f = data_feed.completed_bars(
            cache.get_bars(t, pd.Timestamp("2015-06-01"), SIP_END, strategy.timeframe, feed="sip"),
            strategy.timeframe)
        if len(f) > 300:
            sip[t] = f
    rows = []
    for year in SIMPLE_YEARS:
        start, end = window(year)
        frames = {t: f.loc[start - pd.Timedelta(days=200): end] for t, f in sip.items()
                  if ((f.index >= start) & (f.index <= end)).any()}
        cands = []
        for t, f in frames.items():
            cands += collect_backtest_candidates(f, t, start, end, params, strategy,
                                                 legacy_execution=True)
        result = run_annual_portfolio(
            cands, initial_equity=args.capital, position_fraction=params.position_size_pct,
            max_positions=params.max_concurrent_positions, apply_kill_switch=False, params=params)
        rows.append(detail(year, result, args.capital, benchmark(frames, year)))
        print(f"  simple {year} done", flush=True)
    print_table(f"{args.strategy} @ ${args.capital:,.0f} — simple engine (4h next-bar fills, "
                "no kill switch), 2016–2026", rows, args.capital)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
