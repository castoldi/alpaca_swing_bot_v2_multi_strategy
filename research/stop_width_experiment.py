"""Hypothesis 3: the stops are too tight for bear-market volatility.

2022 attribution: 186 stop-loss exits lost $2,556; 475 time-stop exits *made*
$1,186. If a meaningful share of those stop-outs were whipsaws that would have
recovered, widening the stop converts losses into time-stop gains.

This re-collects candidates with every ``*_stop_loss_pct`` scaled by a
multiplier, so stops widen uniformly across all eight strategies.

Cost of the idea: a wider stop means a bigger loss when the stop is genuinely
right. At 20% position sizing a 10% stop risks 2% of equity per trade; a 20%
stop risks 4%. The experiment measures whether the recovered whipsaws pay for
that.

Usage:
    python research/stop_width_experiment.py
"""
from __future__ import annotations

import dataclasses
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest_portfolio import collect_backtest_candidates, run_annual_portfolio  # noqa: E402
from config import PARAMS, TICKERS  # noqa: E402
from logger_setup import get_logger  # noqa: E402
from strategies import get_enabled, strategy_universe  # noqa: E402

from bear_market_experiment import (  # noqa: E402
    YEARS,
    append_tsv,
    load_ticker_data,
    print_block,
    score_variant,
)

log = get_logger(__name__)

STOP_FIELDS = [
    "stop_loss_pct",
    "breakout_stop_loss_pct",
    "mr_stop_loss_pct",
    "macd_stop_loss_pct",
    "ensemble_stop_loss_pct",
    "sma_cross_stop_loss_pct",
    "tqqq_stop_loss_pct",
]


def scaled_params(multiplier: float):
    """PARAMS with every stop-loss percentage scaled by ``multiplier``."""
    if multiplier == 1.0:
        return PARAMS
    updates = {
        field: min(0.60, getattr(PARAMS, field) * multiplier) for field in STOP_FIELDS
    }
    return dataclasses.replace(PARAMS, **updates)


def run_with_params(ticker_data: dict, params) -> dict[str, dict[int, float]]:
    """Full 11-year run under one parameter set (candidates re-collected)."""
    results: dict[str, dict[int, float]] = {}
    for strategy in get_enabled():
        frames = {
            tk: ticker_data[(tk, strategy.timeframe)]
            for tk in strategy_universe(strategy, TICKERS)
            if not ticker_data[(tk, strategy.timeframe)].empty
        }
        yearly: dict[int, float] = {}
        for year in YEARS:
            ws = pd.Timestamp(date(year, 1, 1))
            we = (
                pd.Timestamp(date(year, 12, 31))
                + pd.Timedelta(days=1)
                - pd.Timedelta(nanoseconds=1)
            )
            cands = []
            for tk, frame in frames.items():
                cands.extend(
                    collect_backtest_candidates(frame, tk, ws, we, params, strategy)
                )
            result = run_annual_portfolio(
                cands,
                initial_equity=params.initial_backtest_equity,
                position_fraction=params.position_size_pct,
                max_positions=params.max_concurrent_positions,
                price_frames=frames,
            )
            yearly[year] = sum(t.pnl_dollars for t in result.trades)
        results[strategy.name] = yearly
    return results


def main() -> int:
    log.info("Loading ticker data...")
    ticker_data = load_ticker_data()

    baseline = score_variant(run_with_params(ticker_data, PARAMS))
    print_block("baseline (stops x1.0)", baseline)

    for mult in (1.25, 1.5, 2.0, 0.75):
        label = f"stops x{mult}"
        scored = score_variant(run_with_params(ticker_data, scaled_params(mult)))
        print_block(label, scored, baseline)
        status = "keep" if scored["score"] > baseline["score"] else "discard"
        append_tsv(label.replace(" ", ""), scored, status, f"all stop_loss_pct x{mult}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
