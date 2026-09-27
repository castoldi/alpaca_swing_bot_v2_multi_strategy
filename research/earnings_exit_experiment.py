"""Stop holding ensemble positions through earnings? (Gap-loss tail.)

The worst ensemble trade at $5k (META -27.4%, Feb 2022) was an earnings gap
through a 9% stop. The live filter only skips entries within 3 trading days of
a report; a 6-session hold (longer when the breakeven-gated time stop waits)
can still carry a position into one. Variants fixed before any result (3 trials):

  E0  current                (candidates exactly as the engine produced them)
  E1  skip entry             also skip entries with a report cutoff inside the
                             next HOLD sessions
  E2  sell before earnings   keep entries; a position still open at the cutoff
                             exits at the close of the last 4h bar that ends by it
  E3  both                   E1 + E2

Cutoff = the last regular close before the report can move the price: the
report day's close for an after-close report, else the previous session's close
(unknown time is treated as pre-open — conservative). Report dates come from the
labelled historical import in cache/earnings.db (every date ever listed).
Baseline candidates keep their own engine exits, so nothing here is re-simulated
except the forced pre-earnings exit.

  realistic  2022-2026: production candidates (minute-fill entries), IEX 4h prices
  simple     2016-2021: legacy candidates on Alpaca SIP 4h

Usage:  python research/earnings_exit_experiment.py [--capital 5000]
"""
from __future__ import annotations

import argparse
import json
import pickle
import sqlite3
import sys
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
from backtest_portfolio import run_annual_portfolio, run_configured_portfolio  # noqa: E402
from config import PARAMS, TICKERS  # noqa: E402
from earnings_calendar import _calendar  # noqa: E402
from market_cache import MarketDataCache  # noqa: E402
from research import capital_size_experiment as cx  # noqa: E402
from research.significance import evaluate  # noqa: E402
from strategies.base import ExitLeg  # noqa: E402

HOLD = PARAMS.ensemble_max_holding_days
VARIANTS = {"E0 current": (False, False), "E1 skip entry": (True, False),
            "E2 sell before": (False, True), "E3 both": (True, True)}
TRIALS = len(VARIANTS) - 1
SIP_END = pd.Timestamp("2026-09-10")


def naive_utc(ts) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_convert("UTC").tz_localize(None) if ts.tzinfo else ts


def cutoffs(ticker: str) -> list[pd.Timestamp]:
    """Sorted naive-UTC regular closes before each report can move the price."""
    with sqlite3.connect(ROOT / "cache" / "earnings.db") as con:
        rows = con.execute("SELECT events FROM snapshots WHERE ticker=?", (ticker,)).fetchall()
    events = {e for (raw,) in rows for e in json.loads(raw)}
    out = set()
    for e in events:
        ts = pd.Timestamp(e)
        if pd.isna(ts):
            continue
        local = ts.tz_localize("America/New_York") if ts.tzinfo is None else ts.tz_convert("America/New_York")
        if local.year < 2015:
            continue
        sched = _calendar(local.year).schedule
        day = pd.Timestamp(local.date())
        i = sched.index.searchsorted(day)
        after_close = local.time().isoformat() != "00:00:00" and local.hour >= 16
        on_session = i < len(sched) and sched.index[i] == day
        j = i if (after_close and on_session) else i - 1
        if 0 <= j < len(sched):
            out.add(naive_utc(sched.iloc[j]["close"]))
    return sorted(out)


def sessions_ahead(ts: pd.Timestamp, n: int) -> pd.Timestamp:
    """Naive-UTC close of the n-th session after ts."""
    local = pd.Timestamp(ts).tz_localize("UTC").tz_convert("America/New_York")
    sched = _calendar(local.year).schedule
    i = sched.index.searchsorted(pd.Timestamp(local.date()))
    return naive_utc(sched.iloc[min(i + n, len(sched) - 1)]["close"])


def next_cutoff(cuts, after: pd.Timestamp):
    i = np.searchsorted(np.array(cuts, dtype="datetime64[ns]"), np.datetime64(after), side="right")
    return cuts[i] if i < len(cuts) else None


def apply(cands, frames, cuts, skip, sell):
    out, forced = [], 0
    for c in cands:
        entry = naive_utc(c.entry_date)
        cut = next_cutoff(cuts[c.ticker], entry)
        if skip and cut is not None and cut <= sessions_ahead(entry, HOLD):
            continue
        leg = c.single_legs[0]
        if sell and cut is not None and naive_utc(leg.exit_date) > cut:
            f = frames[c.ticker]
            ends = f.index + pd.Timedelta(hours=4)
            bars = f[(f.index > entry) & (ends <= cut)]
            if not bars.empty:
                leg = ExitLeg(bars.index[-1] + pd.Timedelta(hours=4), float(bars["close"].iloc[-1]),
                              "earnings_exit", leg.bars_held, 1.0)
                forced += 1
        out.append(replace(c, single_legs=(leg,), scaled_legs=(leg,)))
    return out, forced


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--capital", type=float, default=5000)
    args = parser.parse_args()
    params = replace(PARAMS, initial_backtest_equity=args.capital)
    cache_dir = ROOT / "cache" / "capital_experiment"
    cuts = {t: cutoffs(t) for t in TICKERS}

    books = {}
    cache = MarketDataCache()
    sip = {t: data_feed.completed_bars(cache.get_bars(t, pd.Timestamp("2015-06-01"), SIP_END, "4h",
                                                      feed="sip"), "4h") for t in TICKERS}
    for year in range(2016, 2022):
        path = cache_dir / f"ensemble_legacy_{year}.pkl"
        if not path.exists():
            raise SystemExit("Run research/exit_style_experiment.py first (it caches 2016-2021 candidates).")
        books[year] = (pickle.loads(path.read_bytes()), sip, None)
    for year in cx.YEARS:
        frames = cx.active_frames("ensemble", year)
        books[year] = (cx.candidates("ensemble", year, cache_dir), frames, frames)

    results = {}
    for label, (skip, sell) in VARIANTS.items():
        per_year, rets, forced_total = {}, [], 0
        for year, (cands, frames, valuation) in books.items():
            mod, _ = apply(cands, frames, cuts, skip, sell)
            if valuation is not None:
                res = run_configured_portfolio(mod, params=params, price_frames=valuation)
            else:
                res = run_annual_portfolio(
                    mod, initial_equity=args.capital, position_fraction=params.position_size_pct,
                    max_positions=params.max_concurrent_positions, apply_kill_switch=False, params=params)
            trades = list(res.trades)
            pcts = np.array([t.pnl_pct for t in trades])
            per_year[year] = dict(ret=res.return_pct, dd=compute_max_drawdown(trades, args.capital),
                                  n=len(trades), worst=float(pcts.min()) if len(pcts) else 0.0,
                                  big=int((pcts <= -0.10).sum()))
            rets += [t.pnl_pct for t in trades]
            forced_total += sum(1 for t in trades if t.exit_reason == "earnings_exit")
        results[label] = (per_year, rets, forced_total)
        print(f"  {label} done", flush=True)

    years = list(books)
    print(f"\n{'variant':<18}" + "".join(f"{y:>8}" for y in years)
          + f"{'22-26':>9}{'16-26':>10}{'maxDD':>7}")
    for label, (py, _, _) in results.items():
        comp = lambda ys: np.prod([1 + py[y]["ret"] for y in ys]) - 1  # noqa: E731
        print(f"{label:<18}" + "".join(f"{py[y]['ret'] * 100:>+7.1f}%" for y in years)
              + f"{comp(cx.YEARS) * 100:>+8.1f}%{comp(years) * 100:>+9.1f}%"
              + f"{max(py[y]['dd'] for y in years) * 100:>6.1f}%")
    print(f"\n{'variant':<18}{'trades':>7}{'<=-10%':>7}{'worst':>8}{'sold pre-earn':>14}{'t':>7}{'hurdle':>7}")
    for label, (py, rets, forced) in results.items():
        rep = evaluate(rets, trials=max(TRIALS, 1))
        print(f"{label:<18}{sum(py[y]['n'] for y in years):>7}{sum(py[y]['big'] for y in years):>7}"
              f"{min(py[y]['worst'] for y in years) * 100:>+7.1f}%{forced:>14}{rep.t_stat:>7.2f}"
              f"{rep.hurdle_t:>7.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
