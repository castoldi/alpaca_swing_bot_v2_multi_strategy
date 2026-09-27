"""Win big, lose small? Same ensemble entries, different exits.

The live exit caps winners (TP 4-12%) and allows a 9% stop, so the average win
(+4%) is half the average loss (-9%). Earlier research tried "lose small" alone
(stops x0.75: rejected, research/bear_market_results.tsv) but never "win big".
Variants, fixed before any result was seen (4 trials):

  R0  current: 9% stop, candidate TP, 6-session time stop once at/above entry
  W1  let winners run: 9% stop, no TP, chandelier trail = highest high - 3 x ATR(14)
  W2  win big, lose small: W1 with a 5% stop
  W3  lock in: W1, stop raised to entry once the high reaches +5%

Entries are the strategy's own candidates (entry time and fill price kept);
only the exit is re-simulated, on the strategy's 4h candles, conservatively:
the stop is checked before the bar's high can raise the trail, a gap through
the stop fills at the open, and anything still open at year end exits at the
last close (annual reset). R0 is re-simulated with the same simulator so all
four are compared like-for-like. Every variant then goes through the real
annual portfolio at $5,000.

  realistic  2022-2026: production candidates (minute-fill entries), IEX 4h exits
  simple     2016-2021: legacy 4h candidates on Alpaca SIP 4h

Usage:  python research/exit_style_experiment.py [--capital 5000]
"""
from __future__ import annotations

import argparse
import pickle
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
from backtest_portfolio import (  # noqa: E402
    collect_backtest_candidates, run_annual_portfolio, run_configured_portfolio,
)
from config import PARAMS, TICKERS  # noqa: E402
from market_cache import MarketDataCache  # noqa: E402
from research import capital_size_experiment as cx  # noqa: E402
from research.significance import evaluate  # noqa: E402
from strategies import REGISTRY  # noqa: E402
from strategies.base import ExitLeg, add_indicators  # noqa: E402

VARIANTS = {
    "R0 current": dict(stop=None, tp=True, trail=None, lock=None, time_stop=True),
    "W1 let winners run": dict(stop=None, tp=False, trail=3.0, lock=None, time_stop=False),
    "W2 win big, lose small": dict(stop=0.05, tp=False, trail=3.0, lock=None, time_stop=False),
    "W3 lock in at +5%": dict(stop=None, tp=False, trail=3.0, lock=0.05, time_stop=False),
}
TRIALS = len(VARIANTS)
HOLD_SESSIONS = PARAMS.ensemble_max_holding_days
SIP_END = pd.Timestamp("2026-09-10")


def session_of(ts: pd.Timestamp) -> date:
    return pd.Timestamp(ts).tz_localize("UTC").tz_convert("America/New_York").date() \
        if pd.Timestamp(ts).tzinfo is None else pd.Timestamp(ts).tz_convert("America/New_York").date()


def simulate(c, bars: pd.DataFrame, v: dict, year_end: pd.Timestamp) -> ExitLeg:
    """Re-simulate one exit on 4h bars starting after the entry."""
    entry = c.entry_price
    stop = entry * (1 - v["stop"]) if v["stop"] else c.stop_loss
    tp = c.take_profit if (v["tp"] and c.take_profit > 0) else None
    after = bars[(bars.index > pd.Timestamp(c.entry_date).tz_localize(None)
                  if pd.Timestamp(c.entry_date).tzinfo else bars.index > pd.Timestamp(c.entry_date))
                 & (bars.index <= year_end)]
    if after.empty:
        return ExitLeg(pd.Timestamp(c.entry_date), entry, "end_of_data", 0, 1.0)
    high_water = entry
    sessions = {session_of(c.entry_date)}
    for n, (ts, bar) in enumerate(after.iterrows(), start=1):
        o, h, lo, cl = bar["open"], bar["high"], bar["low"], bar["close"]
        if lo <= stop:                                  # stop first (conservative)
            price = min(o, stop)
            return ExitLeg(ts, price, "gap_stop" if o < stop else "stop_loss", n, 1.0)
        if tp is not None and h >= tp:
            return ExitLeg(ts, max(o, tp), "take_profit", n, 1.0)
        high_water = max(high_water, h)
        if v["lock"] and high_water >= entry * (1 + v["lock"]):
            stop = max(stop, entry)
        if v["trail"] and np.isfinite(bar.get("atr", np.nan)):
            stop = max(stop, high_water - v["trail"] * float(bar["atr"]))
        sessions.add(session_of(ts))
        if v["time_stop"] and len(sessions) - 1 >= HOLD_SESSIONS and cl >= entry:
            return ExitLeg(ts, cl, "time_stop", n, 1.0)
    last = after.iloc[-1]
    return ExitLeg(after.index[-1], float(last["close"]), "end_of_data", len(after), 1.0)


def rebuild(cands, frames, v, year_end):
    out = []
    for c in cands:
        leg = simulate(c, frames[c.ticker], v, year_end)
        out.append(replace(c, take_profit=c.take_profit if v["tp"] else 0.0,
                           single_legs=(leg,), scaled_legs=(leg,)))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--capital", type=float, default=5000)
    args = parser.parse_args()
    strategy = REGISTRY["ensemble"]
    params = replace(PARAMS, initial_backtest_equity=args.capital)
    cache_dir = ROOT / "cache" / "capital_experiment"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # year -> (candidates, indicator frames, valuation frames or None, engine)
    books = {}
    sip_raw = {}
    cache = MarketDataCache()
    for t in TICKERS:
        f = data_feed.completed_bars(
            cache.get_bars(t, pd.Timestamp("2015-06-01"), SIP_END, "4h", feed="sip"), "4h")
        if len(f) > 300:
            sip_raw[t] = f
    for year in range(2016, 2022):
        start = pd.Timestamp(date(year, 1, 1))
        end = start + pd.DateOffset(years=1) - pd.Timedelta(nanoseconds=1)
        frames = {t: f.loc[start - pd.Timedelta(days=200): end] for t, f in sip_raw.items()
                  if ((f.index >= start) & (f.index <= end)).any()}
        path = cache_dir / f"ensemble_legacy_{year}.pkl"
        if path.exists():
            cands = pickle.loads(path.read_bytes())
        else:
            cands = []
            for t, f in frames.items():
                cands += collect_backtest_candidates(f, t, start, end, params, strategy,
                                                     legacy_execution=True)
            path.write_bytes(pickle.dumps(cands))
        print(f"  simple {year}: {len(cands)} candidates", flush=True)
        books[year] = (cands, {t: add_indicators(f, PARAMS) for t, f in frames.items()}, None, "simple")
    for year in cx.YEARS:
        frames = cx.active_frames("ensemble", year)
        books[year] = (cx.candidates("ensemble", year, cache_dir),
                       {t: add_indicators(f, PARAMS) for t, f in frames.items()}, frames, "realistic")

    results = {}
    for label, v in VARIANTS.items():
        per_year, rets = {}, []
        for year, (cands, ind, valuation, engine) in books.items():
            year_end = pd.Timestamp(date(year, 12, 31)) + pd.Timedelta(hours=23)
            rebuilt = rebuild(cands, ind, v, year_end)
            if valuation is not None:
                res = run_configured_portfolio(rebuilt, params=params, price_frames=valuation)
            else:
                res = run_annual_portfolio(
                    rebuilt, initial_equity=args.capital, position_fraction=params.position_size_pct,
                    max_positions=params.max_concurrent_positions, apply_kill_switch=False, params=params)
            trades = list(res.trades)
            pcts = np.array([t.pnl_pct for t in trades])
            per_year[year] = dict(
                ret=res.return_pct, dd=compute_max_drawdown(trades, args.capital), n=len(trades),
                win=float((pcts > 0).mean()) if len(pcts) else 0.0,
                aw=float(pcts[pcts > 0].mean()) if (pcts > 0).any() else 0.0,
                al=float(pcts[pcts <= 0].mean()) if (pcts <= 0).any() else 0.0,
                worst=float(pcts.min()) if len(pcts) else 0.0)
            rets += [(t.entry_date, t.pnl_pct) for t in trades]
        results[label] = (per_year, rets)
        print(f"  {label} done", flush=True)

    years = list(books)
    print(f"\n{'variant':<24}" + "".join(f"{y:>8}" for y in years) + f"{'22-26 comp':>11}{'16-26 comp':>11}{'worst':>7}{'maxDD':>7}")
    for label, (py, _) in results.items():
        comp = lambda ys: np.prod([1 + py[y]["ret"] for y in ys]) - 1  # noqa: E731
        print(f"{label:<24}" + "".join(f"{py[y]['ret'] * 100:>+7.1f}%" for y in years)
              + f"{comp(cx.YEARS) * 100:>+10.1f}%{comp(years) * 100:>+10.1f}%"
              + f"{min(py[y]['ret'] for y in years) * 100:>+6.1f}%{max(py[y]['dd'] for y in years) * 100:>6.1f}%")
    print(f"\n{'variant':<24}{'trades':>7}{'win%':>6}{'avg win':>9}{'avg loss':>9}{'worst trade':>12}"
          f"{'t':>7}{'hurdle':>7}")
    for label, (py, rets) in results.items():
        n = sum(py[y]["n"] for y in years)
        w = lambda k: np.average([py[y][k] for y in years], weights=[py[y]["n"] for y in years])  # noqa: E731
        rep = evaluate([r for _, r in rets], trials=TRIALS)
        print(f"{label:<24}{n:>7}{w('win') * 100:>5.0f}%{w('aw') * 100:>+8.1f}%{w('al') * 100:>+8.1f}%"
              f"{min(py[y]['worst'] for y in years) * 100:>+11.1f}%{rep.t_stat:>7.2f}{rep.hurdle_t:>7.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
