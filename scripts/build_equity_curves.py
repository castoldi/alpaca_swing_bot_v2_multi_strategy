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
    python scripts/build_equity_curves.py --strategy sma_50_cross --start 2008

Daily-timeframe strategies (currently only sma_50_cross) can reach back to
`EARLIEST_DAILY` via yfinance — see `download_daily_history` below. 4h
strategies cannot: Alpaca has no 4h bars before 2016 for this account, and
yfinance has no 4h interval at all, so a `--start` before 2016 for a 4h
strategy simply produces no candidates (and no curve points) for those early
years rather than an error.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_feed                                       # noqa: E402
from backtest_2025 import _MARKET_CACHE, download_history  # noqa: E402
from backtest_portfolio import (                      # noqa: E402
    collect_backtest_candidates,
    run_annual_portfolio,
)
from config import HISTORY_WARMUP_DAYS, PARAMS, TICKERS  # noqa: E402
from dashboard import db as db_mod                    # noqa: E402
from logger_setup import get_logger                   # noqa: E402
from strategies import REGISTRY, get_enabled, strategy_universe  # noqa: E402

log = get_logger(__name__)
EARLIEST = 2016
# The earliest bar Alpaca serves this account for any symbol, any feed —
# confirmed by direct API probing, not documented anywhere. Below this, daily
# history comes from yfinance instead (see yfinance_history.py).
ALPACA_DAILY_FLOOR = date(2016, 1, 1)


def download_daily_history(ticker: str, start: date, end: date) -> pd.DataFrame:
    """Daily bars stitching yfinance (pre-2016) with Alpaca (2016+).

    Half-open boundary at `ALPACA_DAILY_FLOOR`: yfinance is fetched as
    `[start, floor)` and Alpaca as `[floor, end]`, so the two segments cannot
    overlap or gap at the handoff. Confirmed to stitch without a valuation
    jump — same-day closes agree to within 1e-4 across the boundary on
    NVDA/AMZN/AMD, since both sides are fully split+dividend adjusted.
    """
    warmup_start = start - timedelta(days=HISTORY_WARMUP_DAYS)
    segments = []

    if warmup_start < ALPACA_DAILY_FLOOR:
        yf_end = min(end, ALPACA_DAILY_FLOOR)
        if yf_end > warmup_start:
            yf_frame = _MARKET_CACHE.get_bars(
                ticker, warmup_start, yf_end, "1d", feed="yfinance",
            )
            if not yf_frame.empty:
                segments.append(yf_frame)

    alp_start = max(warmup_start, ALPACA_DAILY_FLOOR)
    if end + timedelta(days=1) > alp_start:
        alp_frame = _MARKET_CACHE.get_bars(
            ticker, alp_start, end + timedelta(days=1), "1d", feed="sip",
        )
        if not alp_frame.empty:
            segments.append(alp_frame)

    if not segments:
        return data_feed.completed_bars(pd.DataFrame(), "1d")
    combined = pd.concat(segments)
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    return data_feed.completed_bars(combined, "1d")


def build(strategies, start_year: int, end: date) -> int:
    """Run every strategy-year and persist its equity curve. Returns rows written."""
    written = 0
    for strat in strategies:
        universe = strategy_universe(strat, TICKERS)
        frames = {}
        for ticker in universe:
            if strat.timeframe == "1d":
                # Always the stitched path, even when start_year >= 2016: a
                # January-1 start's 90-day warmup window lands entirely before
                # Alpaca's floor and silently returns nothing, so SMA(50) isn't
                # valid until ~50 trading days into whatever year is first —
                # this cost 2016 6 real trades before the fix (11 vs 17).
                # download_daily_history degrades to a sip-only fetch (no
                # yfinance calls) once warmup no longer crosses the floor, so
                # this costs nothing for a strategy already starting in 2020+.
                frame = download_daily_history(ticker, date(start_year, 1, 1), end)
            else:
                frame = download_history(ticker, date(start_year, 1, 1), end, strat.timeframe)
            # A ticker without enough history to prime the slow SMA would emit
            # no signals anyway; skipping keeps its absence out of the curve.
            # Also the natural way a 4h strategy given a pre-2016 --start ends
            # up starting no earlier than Alpaca's own floor: its frame is
            # simply short until 2016, so early years fall below this bar.
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
