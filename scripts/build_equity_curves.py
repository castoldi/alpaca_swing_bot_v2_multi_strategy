"""Generate and persist per-strategy backtest equity curves for the dashboard.

Backtests reset to `initial_backtest_equity` every January, so the stored curve
is per-year; `db.get_equity_curves` chains completed years into the compounding
"growth of $1" the dashboard race chart plots.

Reuses exactly the same building blocks as the annual backtest runners
(`collect_backtest_candidates` + `run_annual_portfolio`), so a curve can never
disagree with the headline P&L those runners report.

    python scripts/build_equity_curves.py                    # all enabled, 2016+
    python scripts/build_equity_curves.py --strategy ensemble
    python scripts/build_equity_curves.py --start 2020
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest_2025 import download_history            # noqa: E402
from backtest_portfolio import (                      # noqa: E402
    collect_backtest_candidates,
    run_annual_portfolio,
)
from config import PARAMS, TICKERS                    # noqa: E402
from dashboard import db as db_mod                    # noqa: E402
from logger_setup import get_logger                   # noqa: E402
from strategies import REGISTRY, get_enabled, strategy_universe  # noqa: E402

log = get_logger(__name__)
EARLIEST = 2016


def build(strategies, start_year: int, end: date) -> int:
    """Run every strategy-year and persist its equity curve. Returns rows written."""
    written = 0
    for strat in strategies:
        universe = strategy_universe(strat, TICKERS)
        frames = {}
        for ticker in universe:
            frame = download_history(ticker, date(start_year, 1, 1), end, strat.timeframe)
            # A ticker without enough history to prime the slow SMA would emit
            # no signals anyway; skipping keeps its absence out of the curve.
            if not frame.empty and len(frame) >= PARAMS.sma_slow + 5:
                frames[ticker] = frame

        if not frames:
            log.warning("%s: no usable history — skipped", strat.name)
            continue

        for year in range(start_year, end.year + 1):
            t0 = time.time()
            window_start = pd.Timestamp(date(year, 1, 1))
            window_end = (
                pd.Timestamp(min(end, date(year, 12, 31)))
                + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
            )
            candidates = []
            for ticker, frame in frames.items():
                candidates.extend(collect_backtest_candidates(
                    frame, ticker, window_start, window_end, PARAMS, strat,
                ))
            if not candidates:
                continue

            result = run_annual_portfolio(
                candidates,
                initial_equity=PARAMS.initial_backtest_equity,
                position_fraction=PARAMS.position_size_pct,
                max_positions=PARAMS.max_concurrent_positions,
            )
            # Seed the year at its opening equity so a chained curve starts each
            # January exactly where the previous year ended, with no visual gap.
            points = [(window_start.isoformat(), float(PARAMS.initial_backtest_equity))]
            points += [
                (pd.Timestamp(ts).isoformat(), float(eq))
                for ts, eq in result.equity_curve
            ]
            written += db_mod.save_equity_curve(
                strat.name, year, points, PARAMS.initial_backtest_equity,
            )
            log.info(
                "%-16s %d: %3d trades, end $%8.2f (%+6.1f%%), %3d pts [%.1fs]",
                strat.name, year, len(result.trades), result.ending_equity,
                result.return_pct * 100, len(points), time.time() - t0,
            )
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy", choices=list(REGISTRY), default=None)
    parser.add_argument("--start", type=int, default=EARLIEST)
    parser.add_argument("--end", type=lambda s: date.fromisoformat(s), default=date.today())
    args = parser.parse_args()

    strategies = [REGISTRY[args.strategy]] if args.strategy else get_enabled()
    log.info("Building curves for %d strategy/ies from %d", len(strategies), args.start)
    t0 = time.time()
    n = build(strategies, args.start, args.end)
    log.info("Wrote %d curve points in %.1fs", n, time.time() - t0)
    print(f"Wrote {n} equity-curve points across {len(strategies)} strategies.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
