"""Trendline bounce (Tori Trades video): an objective version, backtested.

Write-up: docs/trendline-bounce-2004-2026.md. Source: a discretionary futures
day-trading tutorial (https://youtu.be/TuXOgkcYw9E). The video draws rays through swing lows, buys when price comes back
to the line and reacts, stops "just beyond the line", trails the stop under
each new swing low and exits when a candle closes through the line.

Mechanical translation, fixed before any result was seen:

  swing low  low[j] is the strict minimum of low[j-5 .. j+5]; it is only
             known 5 bars later (confirmed at j+5), so there is no lookahead
  line       through the two most recent confirmed swing lows p1 < p2 with
             low[p2] > low[p1] (a rising line = "trade with the trend")
  rule 1     no 4h close below the line from p1 up to the signal bar
             (wicks may poke through; closes may not "cut" the line)
  entry      signal bar low <= line + 0.25 x ATR(14), close > line, close >
             open (price reached the line and reacted); fill next bar open,
             skipped if it drifts > 1.5% from the signal close (live guard)

Variants (2 trials, plus the baseline):

  E0  ensemble, current exit (re-simulated by exit_style_experiment.simulate
      so every row uses the same 4h exit simulator)
  T1  trendline entry + the video's exit: initial stop = line - 0.25 x ATR,
      raised to (new confirmed swing low - 0.25 x ATR) after each one, exit at
      next open after a 4h close below the entry line. No target, no time stop.
  T2  trendline entry + the ensemble's bracket (9% stop, TP 2.5 x ATR clipped
      to 4-12%, 6-session breakeven time stop) - isolates the entry.
  T3  follow-up after T1/T2 were seen (trial 3): E0 plus T2 candidates in one
      book - does the trendline add anything as a seventh signal source?

Engine: legacy 4h (signal close -> next-bar-open fill), annual $5,000 book,
20% whole-share sizing, 5 slots, realized compounding, year-end close.
Stop is checked before anything else on each bar; a gap through it fills at
the open.

Usage:
  python research/trendline_bounce_experiment.py                       # SIP 2016-2026
  python research/trendline_bounce_experiment.py --feed ibkr --start 2004 --end 2015
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
    BacktestCandidate, collect_backtest_candidates, run_annual_portfolio,
)
from config import PARAMS, TICKERS  # noqa: E402
from market_cache import MarketDataCache  # noqa: E402
from research.exit_style_experiment import VARIANTS as EXIT_VARIANTS, simulate  # noqa: E402
from research.significance import evaluate  # noqa: E402
from strategies import REGISTRY  # noqa: E402
from strategies.base import ExitLeg, add_indicators  # noqa: E402

PIVOT_K = 5
TOUCH_ATR = 0.25
STOP_BUFFER_ATR = 0.25
TRIALS = 3
R0 = EXIT_VARIANTS["R0 current"]


def confirmed_pivots(low: np.ndarray, k: int = PIVOT_K) -> list[tuple[int, int]]:
    """(pivot index, index at which it becomes known), in order."""
    out = []
    for j in range(k, len(low) - k):
        w = low[j - k: j + k + 1]
        if low[j] == w.min() and (w == low[j]).sum() == 1:
            out.append((j, j + k))
    return out


def tl_candidates(f: pd.DataFrame, ticker: str, start: pd.Timestamp, end: pd.Timestamp,
                  p) -> list[BacktestCandidate]:
    o, h, lo, cl = (f[c].to_numpy(float) for c in ("open", "high", "low", "close"))
    atr = f["atr"].to_numpy(float)
    idx = f.index
    pivots = confirmed_pivots(lo)
    known_at = {kn: j for j, kn in pivots}
    in_window = np.where((idx >= start) & (idx <= end))[0]
    if len(in_window) == 0:
        return []
    last = in_window[-1]
    out: list[BacktestCandidate] = []
    known: list[int] = []
    pi = 0
    for i in range(len(f)):
        while pi < len(pivots) and pivots[pi][1] <= i:
            known.append(pivots[pi][0])
            pi += 1
        if i < in_window[0] or i >= last or len(known) < 2 or not np.isfinite(atr[i]):
            continue
        p1, p2 = known[-2], known[-1]
        if lo[p2] <= lo[p1]:
            continue
        slope = (lo[p2] - lo[p1]) / (p2 - p1)
        line = lambda t: lo[p1] + slope * (t - p1)  # noqa: E731
        span = np.arange(p1, i + 1)
        if (cl[span] < lo[p1] + slope * (span - p1)).any():
            continue                                     # rule 1: line already cut
        li = line(i)
        if not (lo[i] <= li + TOUCH_ATR * atr[i] and cl[i] > li and cl[i] > o[i]):
            continue
        e = i + 1
        fill = o[e]
        if abs(fill - cl[i]) / cl[i] > p.entry_max_slippage_pct:
            continue
        stop0 = li - STOP_BUFFER_ATR * atr[i]
        if stop0 >= fill:
            continue
        out.append(dict(ticker=ticker, i=i, e=e, fill=fill, stop0=stop0,
                        p1=p1, slope=slope, atr=atr[i], last=last))
    return [_video_exit(f, c, known_at) for c in out]


def _video_exit(f, c, known_at) -> BacktestCandidate:
    o, lo, cl = (f[k].to_numpy(float) for k in ("open", "low", "close"))
    atr = f["atr"].to_numpy(float)
    stop, e, last = c["stop0"], c["e"], c["last"]
    line = lambda t: lo[c["p1"]] + c["slope"] * (t - c["p1"])  # noqa: E731
    leg = None
    for t in range(e, last + 1):
        if o[t] <= stop:
            leg = ExitLeg(f.index[t], o[t], "gap_stop", t - e, 1.0); break
        if lo[t] <= stop:
            leg = ExitLeg(f.index[t], stop, "stop_loss", t - e, 1.0); break
        if cl[t] < line(t) and t + 1 <= last:
            leg = ExitLeg(f.index[t + 1], o[t + 1], "line_break", t + 1 - e, 1.0); break
        j = known_at.get(t)                              # a swing low confirmed now
        if j is not None and j >= e and np.isfinite(atr[t]):
            stop = max(stop, lo[j] - STOP_BUFFER_ATR * atr[t])
    if leg is None:
        leg = ExitLeg(f.index[last], cl[last], "end_of_data", last - e, 1.0)
    ts = pd.Timestamp(f.index[e])
    return BacktestCandidate(
        ticker=c["ticker"], entry_date=ts, entry_price=c["fill"], stop_loss=c["stop0"],
        take_profit=0.0, strategy="trendline_bounce", single_legs=(leg,), scaled_legs=(leg,),
        signal_date=pd.Timestamp(f.index[c["i"]]), signal_available_at=ts)


def as_bracket(c: BacktestCandidate, f: pd.DataFrame, p, year_end) -> BacktestCandidate:
    """T2: same entry, the ensemble's bracket geometry and exit."""
    a = float(f.loc[c.signal_date, "atr"])
    tp_pct = float(np.clip(p.ensemble_tp_multiple * a / c.entry_price,
                           p.ensemble_tp_floor_pct, p.ensemble_tp_cap_pct))
    b = replace(c, stop_loss=c.entry_price * (1 - p.ensemble_stop_loss_pct),
                take_profit=c.entry_price * (1 + tp_pct))
    leg = simulate(b, f, R0, year_end)
    return replace(b, single_legs=(leg,), scaled_legs=(leg,))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--feed", default="sip", choices=["sip", "ibkr"])
    ap.add_argument("--start", type=int, default=2016)
    ap.add_argument("--end", type=int, default=2026)
    ap.add_argument("--capital", type=float, default=5000)
    args = ap.parse_args()
    p = replace(PARAMS, initial_backtest_equity=args.capital)
    ens = REGISTRY["ensemble"]
    cache = MarketDataCache()
    cache_dir = ROOT / "cache" / "capital_experiment"
    tag = "legacy" if args.feed == "sip" else "ibkr"

    raw = {}
    for t in TICKERS:
        try:
            f = cache.get_bars(t, pd.Timestamp(f"{args.start - 1}-06-01"),
                               pd.Timestamp("2026-09-10"), "4h", feed=args.feed)
        except ValueError:
            continue
        f = data_feed.completed_bars(f, "4h")
        if len(f) > 300:
            raw[t] = f
    print("tickers:", ", ".join(raw), flush=True)

    rows = {"E0 ensemble": {}, "T1 trendline + video exit": {}, "T2 trendline + bracket": {},
            "T3 ensemble + T2": {}}
    for year in range(args.start, args.end + 1):
        start = pd.Timestamp(date(year, 1, 1))
        end = start + pd.DateOffset(years=1) - pd.Timedelta(nanoseconds=1)
        year_end = pd.Timestamp(date(year, 12, 31)) + pd.Timedelta(hours=23)
        frames = {t: f.loc[start - pd.Timedelta(days=200): end] for t, f in raw.items()
                  if ((f.index >= start) & (f.index <= end)).any()}
        ind = {t: add_indicators(f, PARAMS) for t, f in frames.items()}
        path = cache_dir / f"ensemble_{tag}_{year}.pkl"
        if path.exists():
            e0 = pickle.loads(path.read_bytes())
        else:
            e0 = []
            for t, f in frames.items():
                e0 += collect_backtest_candidates(f, t, start, end, p, ens, legacy_execution=True)
            path.write_bytes(pickle.dumps(e0))
        e0 = [c for c in e0 if c.ticker in ind]
        e0 = [replace(c, single_legs=(lg := simulate(c, ind[c.ticker], R0, year_end),),
                      scaled_legs=(lg,)) for c in e0]
        t1 = []
        for t, f in ind.items():
            t1 += tl_candidates(f, t, start, end, p)
        t2 = [as_bracket(c, ind[c.ticker], p, year_end) for c in t1]
        seen = {(c.ticker, c.entry_date) for c in e0}
        t3 = e0 + [c for c in t2 if (c.ticker, c.entry_date) not in seen]
        overlap = sum((c.ticker, c.entry_date) in seen for c in t2)
        for label, cands in zip(rows, (e0, t1, t2, t3)):
            res = run_annual_portfolio(
                cands, initial_equity=args.capital, position_fraction=p.position_size_pct,
                max_positions=p.max_concurrent_positions, apply_kill_switch=False, params=p)
            rows[label][year] = (res, len(cands))
        print(f"  {year}: E0 {len(e0)}  TL {len(t1)} candidates ({overlap} same bar as E0)", flush=True)

    years = list(range(args.start, args.end + 1))
    print(f"\n{'variant':<28}" + "".join(f"{y:>8}" for y in years) + f"{'comp':>11}{'maxDD':>7}")
    for label, py in rows.items():
        comp = np.prod([1 + py[y][0].return_pct for y in years]) - 1
        dd = max(compute_max_drawdown(list(py[y][0].trades), args.capital) for y in years)
        print(f"{label:<28}" + "".join(f"{py[y][0].return_pct * 100:>+7.1f}%" for y in years)
              + f"{comp * 100:>+10.1f}%{dd * 100:>6.1f}%")
    print(f"\n{'variant':<28}{'cands':>7}{'trades':>7}{'win%':>6}{'avg win':>9}{'avg loss':>9}"
          f"{'mean':>8}{'worst':>8}{'hold bars':>10}{'t':>7}{'hurdle':>7}")
    for label, py in rows.items():
        tr = [t for y in years for t in py[y][0].trades]
        pc = np.array([t.pnl_pct for t in tr])
        rep = evaluate(list(pc), trials=TRIALS)
        reasons = pd.Series([t.exit_reason for t in tr]).value_counts().to_dict()
        print(f"{label:<28}{sum(py[y][1] for y in years):>7}{len(tr):>7}{(pc > 0).mean() * 100:>5.0f}%"
              f"{pc[pc > 0].mean() * 100:>+8.1f}%{pc[pc <= 0].mean() * 100:>+8.1f}%"
              f"{pc.mean() * 100:>+7.2f}%{pc.min() * 100:>+7.1f}%"
              f"{np.mean([t.bars_held for t in tr]):>10.1f}{rep.t_stat:>7.2f}{rep.hurdle_t:>7.2f}"
              f"   {reasons}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
