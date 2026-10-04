"""Ensemble stop width on the production engine, 2022-2026 at $5,000.

Follow-up to docs/disaster-stop-2004-2026.md, which used the legacy 4h engine
and found wide stops beat the current 9% stop on return but worsen 2022. This
re-checks one pre-chosen candidate, ``ensemble_stop_loss_pct = 0.20``, on the
engine the yearly runners use (minute execution, earnings filter, kill switch,
annual reset) via the capital-size harness. The baseline reuses that harness's
cached production candidates, so it must reproduce the published +249.6%.

Usage:  python research/stop_width_production.py [--stops 0.09 0.20] [--capital 5000]
"""
from __future__ import annotations

import argparse
import pickle
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest_2025 import compute_max_drawdown  # noqa: E402
from backtest_portfolio import collect_backtest_candidates, run_configured_portfolio  # noqa: E402
from config import PARAMS  # noqa: E402
from research import capital_size_experiment as cx  # noqa: E402
from strategies import REGISTRY  # noqa: E402

CACHE = ROOT / "cache" / "capital_experiment"


def candidates(stop: float, year: int) -> list:
    if stop == PARAMS.ensemble_stop_loss_pct:
        return cx.candidates("ensemble", year, CACHE)
    path = CACHE / f"ensemble_sl{round(stop * 100)}_{year}.pkl"
    if path.exists():
        return pickle.loads(path.read_bytes())
    params = replace(PARAMS, ensemble_stop_loss_pct=stop)
    start, end = cx.window(year)
    found = []
    for ticker, f in cx.active_frames("ensemble", year).items():
        found += collect_backtest_candidates(f, ticker, start, end, params, REGISTRY["ensemble"])
    path.write_bytes(pickle.dumps(found))
    print(f"  stop {stop:.0%} {year}: {len(found)} candidates", flush=True)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stops", nargs="+", type=float, default=[PARAMS.ensemble_stop_loss_pct, 0.20])
    parser.add_argument("--capital", type=float, default=5000)
    args = parser.parse_args()
    years = list(cx.YEARS)
    print(f"\n{'stop':<6}" + "".join(f"{y:>9}" for y in years)
          + f"{'5y comp':>10}{'end $':>9}{'maxDD':>7}{'trades':>7}{'win%':>6}{'avg win':>8}"
          f"{'avg loss':>9}{'worst':>7}{'stops':>6}")
    for stop in args.stops:
        params = replace(PARAMS, ensemble_stop_loss_pct=stop, initial_backtest_equity=args.capital)
        rets, dds, trades = [], [], []
        for year in years:
            res = run_configured_portfolio(candidates(stop, year), params=params,
                                           price_frames=cx.active_frames("ensemble", year))
            t = list(res.trades)
            rets.append(res.return_pct)
            dds.append(compute_max_drawdown(t, args.capital))
            trades += t
        p = np.array([t.pnl_pct for t in trades])
        comp = np.prod([1 + r for r in rets])
        stops = sum(t.exit_reason in ("stop_loss", "gap_stop") for t in trades)
        print(f"{stop:<6.0%}" + "".join(f"{r * 100:>+8.1f}%" for r in rets)
              + f"{(comp - 1) * 100:>+9.1f}%{args.capital * comp:>9,.0f}{max(dds) * 100:>6.1f}%"
              f"{len(p):>7}{(p > 0).mean() * 100:>5.0f}%{p[p > 0].mean() * 100:>+7.1f}%"
              f"{p[p <= 0].mean() * 100:>+8.1f}%{p.min() * 100:>+6.1f}%{stops:>6}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
