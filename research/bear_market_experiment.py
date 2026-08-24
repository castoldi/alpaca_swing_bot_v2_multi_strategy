"""Bear-market research harness — autoresearch-style single-metric experiment runner.

Question under test: can the bot avoid losing money in bear markets while still
making money in bull markets?

Design notes (why this harness exists rather than reusing backtest_history.py):

  * The project's existing research loop (``program.md``) validates on 2025+2026
    only — both bull years. Every "keep" decision it ever made was blind to bear
    markets. This runner scores every year 2016-2026 at once.
  * Candidate collection is the expensive step and does **not** depend on the
    gate, so candidates are collected once and replayed against each variant.
    That makes an A/B sweep cheap.
  * Year classification is by **index** behaviour, never by the bot's own P&L,
    which would be circular.

Usage:
    python research/bear_market_experiment.py                     # baseline
    python research/bear_market_experiment.py --mode drawdown --threshold 0.10
    python research/bear_market_experiment.py --sweep
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest_2025 import compute_max_drawdown, download_history  # noqa: E402
from backtest_portfolio import (  # noqa: E402
    BacktestCandidate,
    collect_backtest_candidates,
    run_annual_portfolio,
)
from config import PARAMS, TICKERS  # noqa: E402
from logger_setup import get_logger  # noqa: E402
from market_regime import build_gate  # noqa: E402
from strategies import get_enabled, strategy_universe  # noqa: E402

log = get_logger(__name__)

START_YEAR = 2016
END_YEAR = 2026
YEARS = list(range(START_YEAR, END_YEAR + 1))

# Classified from S&P 500 calendar-year behaviour (docs/bear-markets-and-crashes.md),
# NOT from this bot's P&L. 2018 closed -6.2% with two corrections (Volmageddon,
# Q4 selloff); 2022 was the -19.4% rate-hike bear. Every other year in range
# closed positive, including 2020 and 2025 which crashed hard mid-year and
# recovered — those are the "gate must not ruin the rebound" stress cases.
BEAR_YEARS = {2018, 2022}
CRASH_RECOVERY_YEARS = {2020, 2025}
BULL_YEARS = [y for y in YEARS if y not in BEAR_YEARS]

RESULTS_TSV = Path(__file__).parent / "bear_market_results.tsv"
TSV_HEADER = "variant\tbear_pnl\tbull_pnl\tworst_year\ttotal_pnl\tscore\tstatus\tdescription"


def load_ticker_data() -> dict[tuple[str, str], pd.DataFrame]:
    """Fetch every ticker/timeframe the enabled strategies need, once."""
    strategies = get_enabled()
    timeframes = sorted({s.timeframe for s in strategies})
    needed = sorted({t for s in strategies for t in strategy_universe(s, TICKERS)})
    data: dict[tuple[str, str], pd.DataFrame] = {}
    for timeframe in timeframes:
        for ticker in needed:
            frame = download_history(
                ticker, date(START_YEAR, 1, 1), date(END_YEAR, 12, 31), timeframe
            )
            if frame.empty or len(frame) < PARAMS.sma_slow + 5:
                frame = pd.DataFrame()
            data[(ticker, timeframe)] = frame
    return data


def collect_all_candidates(
    ticker_data: dict[tuple[str, str], pd.DataFrame],
) -> tuple[dict, dict]:
    """Collect candidates per strategy per year once; gates replay against these.

    Returns ``(candidates[strategy][year], frames[strategy][ticker])``.
    """
    candidates: dict[str, dict[int, list[BacktestCandidate]]] = {}
    frames_by_strategy: dict[str, dict[str, pd.DataFrame]] = {}
    for strategy in get_enabled():
        frames = {
            ticker: ticker_data[(ticker, strategy.timeframe)]
            for ticker in strategy_universe(strategy, TICKERS)
            if not ticker_data[(ticker, strategy.timeframe)].empty
        }
        frames_by_strategy[strategy.name] = frames
        per_year: dict[int, list[BacktestCandidate]] = {}
        for year in YEARS:
            window_start = pd.Timestamp(date(year, 1, 1))
            window_end = (
                pd.Timestamp(date(year, 12, 31))
                + pd.Timedelta(days=1)
                - pd.Timedelta(nanoseconds=1)
            )
            found: list[BacktestCandidate] = []
            for ticker, frame in frames.items():
                found.extend(
                    collect_backtest_candidates(
                        frame, ticker, window_start, window_end, PARAMS, strategy
                    )
                )
            per_year[year] = found
        candidates[strategy.name] = per_year
        log.info(
            "%s: %d candidates across %d years",
            strategy.name,
            sum(len(v) for v in per_year.values()),
            len(YEARS),
        )
    return candidates, frames_by_strategy


_TICKER_GATES: dict[tuple[str, str, float], object] = {}


def ticker_gate(ticker: str, mode: str, threshold: float):
    """Per-name regime gate, cached. Releases per ticker, not market-wide."""
    key = (ticker, mode, threshold)
    if key not in _TICKER_GATES:
        _TICKER_GATES[key] = build_gate(
            date(START_YEAR, 1, 1), date(END_YEAR, 12, 31), mode, threshold, ticker=ticker
        )
    return _TICKER_GATES[key]


def filter_by_ticker_trend(
    year_candidates: list[BacktestCandidate], mode: str, threshold: float
) -> list[BacktestCandidate]:
    """Drop candidates whose own ticker is in a confirmed downtrend."""
    kept = []
    for candidate in year_candidates:
        gate = ticker_gate(candidate.ticker, mode, threshold)
        if not gate.blocked(candidate.entry_date):
            kept.append(candidate)
    return kept


def run_variant(
    candidates: dict,
    frames_by_strategy: dict,
    mode: str,
    threshold: float,
    scope: str = "market",
) -> dict[str, dict[int, float]]:
    """Run every strategy/year under one gate configuration.

    ``scope`` is ``"market"`` (one SPY-wide verdict, blocks every ticker) or
    ``"ticker"`` (each name gated on its own trend, so healthy names keep
    trading while broken ones are skipped).
    """
    market_gate = None
    if scope == "market" and mode != "off":
        market_gate = build_gate(date(START_YEAR, 1, 1), date(END_YEAR, 12, 31), mode, threshold)
        log.info(
            "Market gate '%s' (thr %.2f) active on %.1f%% of days",
            mode, threshold, market_gate.blocked_fraction * 100,
        )
    results: dict[str, dict[int, float]] = {}
    for strategy_name, per_year in candidates.items():
        frames = frames_by_strategy[strategy_name]
        yearly: dict[int, float] = {}
        for year, year_candidates in per_year.items():
            if scope == "ticker" and mode != "off":
                year_candidates = filter_by_ticker_trend(year_candidates, mode, threshold)
            result = run_annual_portfolio(
                year_candidates,
                initial_equity=PARAMS.initial_backtest_equity,
                position_fraction=PARAMS.position_size_pct,
                max_positions=PARAMS.max_concurrent_positions,
                price_frames=frames,
                regime_gate=market_gate,
            )
            yearly[year] = sum(t.pnl_dollars for t in result.trades)
        results[strategy_name] = yearly
    return results


def score_variant(results: dict[str, dict[int, float]]) -> dict:
    """Reduce a variant to the metric block.

    Portfolio view = the sum across all strategies for each year, i.e. what an
    operator running the whole bot would actually experience.

    ``score`` weights bear-market dollars 2x bull-market dollars. That weight is
    a *stated preference* (losing capital in a drawdown is worse than missing
    upside), fixed before any experiment ran — not a tuned knob.
    """
    per_year = {
        year: sum(results[s][year] for s in results) for year in YEARS
    }
    bear_pnl = sum(per_year[y] for y in BEAR_YEARS)
    bull_pnl = sum(per_year[y] for y in BULL_YEARS)
    worst_year = min(per_year.values())
    total = sum(per_year.values())
    return {
        "per_year": per_year,
        "per_strategy": results,
        "bear_pnl": bear_pnl,
        "bull_pnl": bull_pnl,
        "worst_year": worst_year,
        "total_pnl": total,
        "score": bull_pnl + 2.0 * bear_pnl,
    }


def print_block(label: str, scored: dict, baseline: dict | None = None) -> None:
    print(f"\n{'=' * 78}")
    print(f"VARIANT: {label}")
    print("=" * 78)
    header = "Year".ljust(10) + "".join(f"{y:>10}" for y in YEARS)
    print(header)
    cells = "".join(f"{scored['per_year'][y]:>+10.0f}" for y in YEARS)
    print("P&L".ljust(10) + cells)
    if baseline is not None:
        delta = "".join(
            f"{scored['per_year'][y] - baseline['per_year'][y]:>+10.0f}" for y in YEARS
        )
        print("vs base".ljust(10) + delta)
    print("-" * 78)
    for key in ("bear_pnl", "bull_pnl", "worst_year", "total_pnl", "score"):
        line = f"{key:<18} {scored[key]:>+12.2f}"
        if baseline is not None:
            line += f"   (base {baseline[key]:>+10.2f}, delta {scored[key] - baseline[key]:>+10.2f})"
        print(line)


def append_tsv(variant: str, scored: dict, status: str, description: str) -> None:
    if not RESULTS_TSV.exists():
        RESULTS_TSV.write_text(TSV_HEADER + "\n", encoding="utf-8")
    row = (
        f"{variant}\t{scored['bear_pnl']:.2f}\t{scored['bull_pnl']:.2f}\t"
        f"{scored['worst_year']:.2f}\t{scored['total_pnl']:.2f}\t"
        f"{scored['score']:.2f}\t{status}\t{description}\n"
    )
    with RESULTS_TSV.open("a", encoding="utf-8") as handle:
        handle.write(row)


def main() -> int:
    parser = argparse.ArgumentParser(description="Bear-market gate experiment")
    parser.add_argument("--mode", default="off", choices=["off", "drawdown", "sma200"])
    parser.add_argument("--threshold", type=float, default=0.10)
    parser.add_argument("--sweep", action="store_true", help="Run the full variant sweep")
    args = parser.parse_args()

    log.info("Loading ticker data %d-%d...", START_YEAR, END_YEAR)
    ticker_data = load_ticker_data()
    log.info("Collecting candidates (once; replayed per variant)...")
    candidates, frames_by_strategy = collect_all_candidates(ticker_data)

    baseline = score_variant(run_variant(candidates, frames_by_strategy, "off", 0.0))
    print_block("baseline (no gate)", baseline)
    append_tsv("baseline", baseline, "baseline", "no market regime gate")

    if not args.sweep:
        if args.mode == "off":
            return 0
        scored = score_variant(
            run_variant(candidates, frames_by_strategy, args.mode, args.threshold)
        )
        label = f"{args.mode}@{args.threshold:.2f}"
        print_block(label, scored, baseline)
        status = "keep" if scored["score"] > baseline["score"] else "discard"
        append_tsv(label, scored, status, f"gate mode={args.mode} thr={args.threshold}")
        return 0

    variants = [
        ("market", "drawdown", 0.10),
        ("market", "sma200", 0.0),
        ("market", "sma50", 0.0),
        ("ticker", "sma200", 0.0),
        ("ticker", "sma50", 0.0),
        ("ticker", "drawdown", 0.10),
        ("ticker", "drawdown", 0.20),
    ]
    for scope, mode, threshold in variants:
        scored = score_variant(
            run_variant(candidates, frames_by_strategy, mode, threshold, scope=scope)
        )
        label = f"{scope}:{mode}" + (f"@{threshold:.2f}" if mode == "drawdown" else "")
        print_block(label, scored, baseline)
        status = "keep" if scored["score"] > baseline["score"] else "discard"
        append_tsv(label, scored, status, f"scope={scope} mode={mode} thr={threshold}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
