"""No stop-loss: take profit only, then re-enter on the next signal.

Question (2026-10-03): what if the bot never sold at a loss? Every entry keeps
its take-profit; the stop is removed. A position that never reaches its target
just stays open, holding one of the five slots. Re-entry is the normal rule:
once a target fills, the next signal on that ticker can open a new position.

Variants, fixed before any result was seen (2 new x 6 bracket strategies = 12 trials):

  S0  current: stop + TP + time stop (exits only at/above entry after N sessions)
  N1  no stop: TP + the same time stop, so a trade only ever closes at/above entry
  N2  no stop, TP only: hold until the target fills, however long that takes

Unlike the annual backtests this is ONE continuous book from 2016-01 to the end
of the SIP cache at $5,000: no January reset, because a reset would force-sell
the underwater positions and act as a yearly stop. Entries are the strategies'
own legacy candidates on Alpaca SIP 4h (one engine for every year); exits are
re-simulated on the full history with strategies.base.simulate_exit (S0, N1) or
a target-only loop (N2). Positions go through the real portfolio engine (20%
whole-share sizing, 5 slots, one per ticker, realized compounding). Yearly
returns and drawdowns are mark-to-market on daily closes, so a stuck position's
paper loss counts. Open positions at the end are valued at the last close.

Buy-and-hold rows: QQQ, SPY, and an equal-weight basket of the universe names
listed in January 2016 (NVDA, AMZN, META, AMD), bought once and never touched.
The universe was picked in 2026 and holds the decade's biggest winners, so that
basket (and every strategy here) carries heavy hindsight bias.

Stress test on the 2000-2002 and 2008 bears (IB Gateway import, feed "ibkr"; META
listed 2012, so the basket is NVDA/AMZN/AMD until then):

  python research/no_stop_experiment.py --feed ibkr --start 2000-01-01 --end 2015-12-31

Usage:  python research/no_stop_experiment.py [--strategies ensemble ...] [--capital 5000]
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
from backtest_portfolio import collect_backtest_candidates, run_annual_portfolio  # noqa: E402
from config import PARAMS, TICKERS  # noqa: E402
from market_cache import MarketDataCache  # noqa: E402
from research.significance import evaluate  # noqa: E402
from strategies import REGISTRY  # noqa: E402
from strategies.base import EntrySignal, ExitLeg, add_indicators, simulate_exit  # noqa: E402

BRACKET = ["ensemble", "regime", "momentum_macd", "trend_pullback", "breakout", "mean_reversion"]
VARIANTS = ["S0 current (stop)", "N1 no stop + time stop", "N2 no stop, TP only"]
TRIALS = 2 * len(BRACKET)
START, END = pd.Timestamp("2016-01-01"), pd.Timestamp("2026-09-10")
BASKET = ["NVDA", "AMZN", "META", "AMD"]


def legacy_candidates(name: str, frames: dict, cache_dir: Path, feed: str) -> list:
    out = []
    tag = "legacy" if feed == "sip" else feed
    for year in range(START.year, END.year + 1):
        path = cache_dir / f"{name}_{tag}_{year}.pkl"
        if path.exists():
            out += pickle.loads(path.read_bytes())
            continue
        start = pd.Timestamp(date(year, 1, 1))
        end = start + pd.DateOffset(years=1) - pd.Timedelta(nanoseconds=1)
        found = []
        for t, f in frames.items():
            sl = f.loc[start - pd.Timedelta(days=200): end]
            if ((sl.index >= start) & (sl.index <= end)).any():
                found += collect_backtest_candidates(sl, t, start, end, PARAMS, REGISTRY[name],
                                                     legacy_execution=True)
        path.write_bytes(pickle.dumps(found))
        print(f"  {name} {year}: {len(found)} candidates", flush=True)
        out += found
    return out


def resimulate(c, data: pd.DataFrame, variant: str) -> ExitLeg:
    idx = data.index.get_loc(pd.Timestamp(c.entry_date))
    if variant.startswith("N2"):
        after = data.iloc[idx:]
        hit = np.flatnonzero(after["high"].to_numpy() >= c.take_profit)
        if hit.size:
            i = int(hit[0])
            o = float(after["open"].iloc[i])
            return ExitLeg(after.index[i], max(o, c.take_profit) if i else c.take_profit,
                           "take_profit", i, 1.0)
        return ExitLeg(after.index[-1], float(after["close"].iloc[-1]), "end_of_data",
                       len(after) - 1, 1.0)
    stop = c.stop_loss if variant.startswith("S0") else -np.inf
    sig = EntrySignal(date=pd.Timestamp(c.entry_date), entry_price=c.entry_price, stop_loss=stop,
                      take_profit=c.take_profit, atr=0.0, rsi=0.0, strategy=c.strategy)
    when, price, reason, bars = simulate_exit(data, idx, sig, PARAMS, entry_at_open=True)
    return ExitLeg(pd.Timestamp(when), float(price), reason, bars, 1.0)


def mtm_curve(trades, closes: pd.DataFrame, capital: float) -> pd.Series:
    """Daily mark-to-market equity: realized P&L plus open positions at the close."""
    eq = pd.Series(capital, index=closes.index, dtype=float)
    for t in trades:
        entry_day = pd.Timestamp(t.entry_date).normalize()
        exit_day = pd.Timestamp(t.exit_date).normalize()
        held = (closes.index >= entry_day) & (closes.index < exit_day)
        eq[held] += t.shares * (closes.loc[held, t.ticker] - t.entry_price)
        eq[closes.index >= exit_day] += t.pnl_dollars
    return eq


def stats(eq: pd.Series) -> dict:
    yearly = eq.groupby(eq.index.year).last()
    prev = pd.concat([pd.Series([eq.iloc[0]], index=[yearly.index[0] - 1]), yearly]).shift(1).iloc[1:]
    rets = yearly / prev - 1
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    dd = (eq / eq.cummax() - 1).min()
    return dict(yearly=rets, total=eq.iloc[-1] / eq.iloc[0] - 1,
                cagr=(eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1, dd=dd, end=eq.iloc[-1])


def main() -> int:
    global START, END
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strategies", nargs="+", default=BRACKET)
    parser.add_argument("--capital", type=float, default=5000)
    parser.add_argument("--feed", default="sip", choices=["sip", "ibkr"])
    parser.add_argument("--start", default=str(START.date()))
    parser.add_argument("--end", default=str(END.date()))
    args = parser.parse_args()
    START, END = pd.Timestamp(args.start), pd.Timestamp(args.end)
    cache_dir = ROOT / "cache" / "capital_experiment"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = MarketDataCache()

    raw = {}
    for t in TICKERS:
        try:
            f = data_feed.completed_bars(
                cache.get_bars(t, START - pd.Timedelta(days=220), END, "4h", feed=args.feed), "4h")
        except ValueError as exc:  # ibkr is import-only: a symbol never imported is skipped
            print(f"  {t}: no {args.feed} 4h history ({exc})")
            continue
        if len(f) > 300:
            raw[t] = f
    ind = {t: add_indicators(f, PARAMS) for t, f in raw.items()}
    closes = pd.DataFrame({t: f["close"].groupby(f.index.normalize()).last() for t, f in raw.items()})
    basket = [t for t in BASKET if t in closes and closes[t].loc[START:].notna().iloc[:5].any()]
    closes = closes.loc[START:].ffill().dropna(subset=basket)  # first day the basket traded

    rows = {}
    for name in args.strategies:
        cands = legacy_candidates(name, raw, cache_dir, args.feed)
        for variant in VARIANTS:
            rebuilt = []
            for c in cands:
                leg = resimulate(c, ind[c.ticker], variant)
                rebuilt.append(replace(c, single_legs=(leg,), scaled_legs=(leg,)))
            res = run_annual_portfolio(
                rebuilt, initial_equity=args.capital, position_fraction=PARAMS.position_size_pct,
                max_positions=PARAMS.max_concurrent_positions, apply_kill_switch=False, params=PARAMS)
            trades = list(res.trades)
            eq = mtm_curve(trades, closes, args.capital)
            pcts = np.array([t.pnl_pct for t in trades])
            days = np.array([(pd.Timestamp(t.exit_date) - pd.Timestamp(t.entry_date)).days for t in trades])
            still_open = [t for t in trades if t.exit_reason == "end_of_data"]
            rows[(name, variant)] = dict(
                **stats(eq), n=len(trades), win=float((pcts > 0).mean()) if len(pcts) else 0,
                worst=float(pcts.min()) if len(pcts) else 0, max_days=int(days.max()) if len(days) else 0,
                med_days=float(np.median(days)) if len(days) else 0,
                open=[(t.ticker, str(pd.Timestamp(t.entry_date).date()), round(t.pnl_pct * 100, 1))
                      for t in still_open],
                t=evaluate(list(pcts), trials=TRIALS) if len(pcts) > 2 else None)
            print(f"  {name} / {variant}: done", flush=True)

    # Buy and hold on the same daily closes.
    spy_qqq = {}
    for t in ["QQQ", "SPY"]:
        d = cache.get_bars(t, START, END, "1d", feed=args.feed)
        spy_qqq[t] = d["close"].groupby(d.index.normalize()).last()
    bh = pd.DataFrame(spy_qqq).reindex(closes.index).ffill().bfill()
    bh[f"B&H basket ({'/'.join(basket)})"] = (closes[basket] / closes[basket].iloc[0]).mean(axis=1)
    for col in bh:
        rows[(col if col.startswith("B&H") else f"B&H {col}", "")] = dict(
            **stats(bh[col] / bh[col].iloc[0] * args.capital), n=None)

    years = list(range(START.year, END.year + 1))
    print(f"\nContinuous book {START.date()} -> {closes.index[-1].date()}, ${args.capital:,.0f}, "
          f"mark-to-market yearly returns")
    print(f"{'portfolio':<44}" + "".join(f"{y:>7}" for y in years)
          + f"{'total':>10}{'CAGR':>7}{'maxDD':>7}{'end $':>10}")
    for (name, variant), r in rows.items():
        label = f"{name} | {variant}" if variant else name
        print(f"{label:<44}" + "".join(f"{r['yearly'].get(y, np.nan) * 100:>+6.0f}%" for y in years)
              + f"{r['total'] * 100:>+9.0f}%{r['cagr'] * 100:>+6.1f}%{r['dd'] * 100:>6.0f}%{r['end']:>10,.0f}")
    print(f"\n{'portfolio':<44}{'trades':>7}{'win%':>6}{'worst':>8}{'med d':>7}{'max d':>7}{'t':>7}{'hurdle':>7}  open at end")
    for (name, variant), r in rows.items():
        if r["n"] is None:
            continue
        rep = r["t"]
        print(f"{name + ' | ' + variant:<44}{r['n']:>7}{r['win'] * 100:>5.0f}%{r['worst'] * 100:>+7.1f}%"
              f"{r['med_days']:>7.0f}{r['max_days']:>7}{(rep.t_stat if rep else 0):>7.2f}"
              f"{(rep.hurdle_t if rep else 0):>7.2f}  {r['open']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
