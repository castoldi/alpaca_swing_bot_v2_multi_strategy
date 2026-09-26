"""Does starting capital change ensemble's results? Live-readiness Phase 2.

Every tuned number in this project came from $1,000 annual backtests, where a
20% slot is $200 and whole-share sizing skips most of the universe. The user's
planned live start is ~$5,000. Candidates (signals + exits) do not depend on
capital, so they are collected once per year on the production engine
(the yearly runners' path: minute execution, earnings filter, kill switch) and
replayed through the annual portfolio at each capital level.

Usage:  python research/capital_size_experiment.py [--strategy ensemble] [--capitals 1000 5000 100000]
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest_2025 import compute_max_drawdown, download_history  # noqa: E402
from backtest_portfolio import collect_backtest_candidates, run_configured_portfolio  # noqa: E402
from config import PARAMS, TICKERS  # noqa: E402
from strategies import REGISTRY, strategy_universe  # noqa: E402

YEARS = range(2022, 2027)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strategy", default="ensemble")
    parser.add_argument("--capitals", nargs="+", type=float, default=[1000, 5000, 100000])
    args = parser.parse_args()
    strategy = REGISTRY[args.strategy]

    frames = {}
    for ticker in strategy_universe(strategy, TICKERS):
        frame = download_history(ticker, date(YEARS[0], 1, 1), date.today(), strategy.timeframe)
        if not frame.empty and len(frame) >= PARAMS.sma_slow + 5:
            frames[ticker] = frame

    rows = []
    for year in YEARS:
        start = pd.Timestamp(date(year, 1, 1))
        end = pd.Timestamp(date(year, 12, 31)) + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
        candidates = []
        # Tickers with bars this year (ARM listed 2023-09).
        active = {t: f for t, f in frames.items()
                  if ((f.index >= start) & (f.index <= end)).any()}
        for ticker, frame in active.items():
            candidates += collect_backtest_candidates(frame, ticker, start, end, PARAMS, strategy)
        print(f"{year}: {len(candidates)} candidates", flush=True)
        for cap in args.capitals:
            params = replace(PARAMS, initial_backtest_equity=cap)
            result = run_configured_portfolio(candidates, params=params, price_frames=active)
            trades = list(result.trades)
            by_ticker = {t: sum(1 for x in trades if x.ticker == t) for t in frames}
            rows.append(dict(
                year=year, capital=cap, ret=result.return_pct,
                dd=compute_max_drawdown(trades, cap), trades=len(trades),
                skipped=result.skipped_positions,
                mix=" ".join(f"{t}:{n}" for t, n in by_ticker.items()),
            ))

    print(f"\n{'year':>5} {'capital':>9} {'return':>8} {'max DD':>7} {'trades':>6} {'skipped':>7}  per ticker")
    for r in rows:
        print(f"{r['year']:>5} {r['capital']:>9,.0f} {r['ret'] * 100:>+7.1f}% {r['dd'] * 100:>6.1f}% "
              f"{r['trades']:>6} {r['skipped']:>7}  {r['mix']}")
    for cap in args.capitals:
        sel = [r for r in rows if r["capital"] == cap]
        comp = 1.0
        for r in sel:
            comp *= 1 + r["ret"]
        print(f"capital {cap:>9,.0f}: compounded {len(sel)}y {(comp - 1) * 100:+.1f}%, "
              f"worst year {min(r['ret'] for r in sel) * 100:+.1f}%, "
              f"worst DD {max(r['dd'] for r in sel) * 100:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
