"""Hypothesis 2: volatility-targeted position sizing, not entry gating.

The 2022 attribution (research/diagnose_2022.py) showed the bot's only losing
year is driven entirely by stop-losses: 186 stop-outs cost $2,556 while wins
returned $1,932. Entry gates (hypothesis 1) cut those stops only by cutting all
trading, and every variant gave up 10-16x more bull-market profit than it saved.

This tests the continuous alternative: keep taking every signal, but scale
position size by the ticker's recent realized volatility. Bear markets are
high-volatility, so size shrinks automatically without anyone predicting a
regime; calm bull tape keeps full size.

    scale = clip(target_vol / realized_vol, lo, hi)

``realized_vol`` is the annualized stdev of the ticker's last 20 daily returns,
read strictly from bars that closed before the entry date (no lookahead).

Usage:
    python research/vol_target_experiment.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest_portfolio import run_annual_portfolio  # noqa: E402
from config import PARAMS  # noqa: E402
from logger_setup import get_logger  # noqa: E402
from market_cache import MarketDataCache  # noqa: E402

from bear_market_experiment import (  # noqa: E402
    END_YEAR,
    START_YEAR,
    YEARS,
    append_tsv,
    collect_all_candidates,
    load_ticker_data,
    print_block,
    score_variant,
)

log = get_logger(__name__)

VOL_WINDOW = 20  # trading days of realized vol
TRADING_DAYS = 252


class VolLookup:
    """Point-in-time annualized realized volatility per ticker."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    def _load(self, ticker: str) -> tuple[np.ndarray, np.ndarray]:
        if ticker not in self._cache:
            cache = MarketDataCache()
            bars = cache.get_bars(
                ticker,
                date(START_YEAR, 1, 1) - timedelta(days=200),
                date(END_YEAR, 12, 31) + timedelta(days=1),
                "1d",
                feed="sip",
            )
            if bars.empty:
                self._cache[ticker] = (np.array([], dtype="datetime64[D]"), np.array([]))
            else:
                close = bars["close"].astype(float)
                vol = (
                    close.pct_change()
                    .rolling(VOL_WINDOW, min_periods=VOL_WINDOW)
                    .std()
                    * np.sqrt(TRADING_DAYS)
                )
                days = np.array(
                    [np.datetime64(pd.Timestamp(t).date(), "D") for t in bars.index],
                    dtype="datetime64[D]",
                )
                self._cache[ticker] = (days, vol.to_numpy(dtype=float))
        return self._cache[ticker]

    def realized(self, ticker: str, timestamp) -> float:
        days, vol = self._load(ticker)
        if days.size == 0:
            return float("nan")
        as_of = np.datetime64(pd.Timestamp(timestamp).date(), "D")
        idx = int(np.searchsorted(days, as_of, side="left")) - 1
        if idx < 0:
            return float("nan")
        return float(vol[idx])


def make_sizer(lookup: VolLookup, target: float, lo: float, hi: float):
    """Return a position_fraction_fn implementing volatility targeting."""
    base = PARAMS.position_size_pct

    def sizer(candidate) -> float:
        realized = lookup.realized(candidate.ticker, candidate.entry_date)
        if not np.isfinite(realized) or realized <= 0:
            return base  # fail to normal size, never to zero
        scale = min(hi, max(lo, target / realized))
        return base * scale

    return sizer


def run_vol_variant(
    candidates: dict, frames_by_strategy: dict, sizer
) -> dict[str, dict[int, float]]:
    results: dict[str, dict[int, float]] = {}
    for strategy_name, per_year in candidates.items():
        frames = frames_by_strategy[strategy_name]
        yearly: dict[int, float] = {}
        for year, year_candidates in per_year.items():
            result = run_annual_portfolio(
                year_candidates,
                initial_equity=PARAMS.initial_backtest_equity,
                position_fraction=PARAMS.position_size_pct,
                max_positions=PARAMS.max_concurrent_positions,
                price_frames=frames,
                position_fraction_fn=sizer,
            )
            yearly[year] = sum(t.pnl_dollars for t in result.trades)
        results[strategy_name] = yearly
    return results


def main() -> int:
    log.info("Loading ticker data...")
    ticker_data = load_ticker_data()
    candidates, frames_by_strategy = collect_all_candidates(ticker_data)

    baseline = score_variant(run_vol_variant(candidates, frames_by_strategy, None))
    print_block("baseline (flat 20% sizing)", baseline)

    lookup = VolLookup()
    # Report the realized-vol distribution so the target is not a magic number.
    samples = []
    for per_year in candidates.values():
        for year_candidates in per_year.values():
            for c in year_candidates[:: max(1, len(year_candidates) // 40 or 1)]:
                v = lookup.realized(c.ticker, c.entry_date)
                if np.isfinite(v):
                    samples.append(v)
    if samples:
        arr = np.array(samples)
        print(
            f"\nRealized 20d vol across signals: "
            f"p25={np.percentile(arr, 25):.2f} median={np.median(arr):.2f} "
            f"p75={np.percentile(arr, 75):.2f} p95={np.percentile(arr, 95):.2f}"
        )

    variants = [
        ("volt@0.40/1.0", 0.40, 0.35, 1.0),
        ("volt@0.45/1.0", 0.45, 0.35, 1.0),
        ("volt@0.50/1.0", 0.50, 0.35, 1.0),
        ("volt@0.45/1.3", 0.45, 0.35, 1.3),
        ("volt@0.50/1.5", 0.50, 0.35, 1.5),
        ("volt@0.55/1.5", 0.55, 0.35, 1.5),
    ]
    for label, target, lo, hi in variants:
        sizer = make_sizer(lookup, target, lo, hi)
        scored = score_variant(run_vol_variant(candidates, frames_by_strategy, sizer))
        print_block(label, scored, baseline)
        status = "keep" if scored["score"] > baseline["score"] else "discard"
        append_tsv(label, scored, status, f"vol target={target} clip=[{lo},{hi}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
