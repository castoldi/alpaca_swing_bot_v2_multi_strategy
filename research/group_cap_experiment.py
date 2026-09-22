"""Correlated-group exposure caps (R04 in docs/code-review-2026-09-22.md).

The universe is five mega-cap tech/semiconductor names and the portfolio has
five 20% slots, so its natural full state is 100% of equity in one factor. This
experiment asks whether capping a correlated group buys enough drawdown
protection to be worth the P&L it gives up.

Predeclared panel — every variant is reported and counted, losers included:

    A  none         live today: no group cap
    B  semis_40     NVDA+AMD+ARM together <= 40% of equity (two full slots)
    C  semis_20     NVDA+AMD+ARM together <= 20% (one slot)
    D  all_60       all five names together <= 60% (three slots)
    E  all_40       all five names together <= 40% (two slots)

Strategy: the live Ensemble. Years: 2022-2026 (IEX 4h + minute execution),
one frozen dataset per year; candidates are collected once per year and every
variant runs the same annual portfolio runner (sizing, kill switch, tax guard)
with only ``exposure_groups`` changed. Equity starts at $100,000 — about the
live account — because whole-share sizing at the usual $1,000 distorts a cap
measured in dollars.

Decision rule (fixed before running): a cap is adopted only if, against A,
  * its max drawdown is lower in at least 4 of the 5 years, AND
  * its total P&L is at least 75% of A's, AND
  * its return-to-drawdown ratio (total P&L / mean max drawdown) beats A's.
The search-corrected significance report over all 5 variants is recorded as
evidence either way; a risk cap is not expected to win on mean return.

Usage:
    python research/group_cap_experiment.py
    python research/group_cap_experiment.py --years 2025 2026 --no-log
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest_portfolio import collect_backtest_candidates, run_configured_portfolio  # noqa: E402
from config import PARAMS, TICKERS, StrategyType  # noqa: E402
from logger_setup import get_logger  # noqa: E402
from research.optimizer import load_dataset  # noqa: E402
from research.significance import evaluate_search, returns_from_trades  # noqa: E402
from strategies import REGISTRY, strategy_universe  # noqa: E402

log = get_logger(__name__)
REPORT_JSON = ROOT / "reports" / "group_cap_experiment.json"
SEMIS = ("NVDA", "AMD", "ARM")
ALL = tuple(TICKERS)
START_EQUITY = 100_000.0

PANEL = {
    "A_none": (),
    "B_semis_40": (("semis", SEMIS, 0.40),),
    "C_semis_20": (("semis", SEMIS, 0.20),),
    "D_all_60": (("megacap_tech", ALL, 0.60),),
    "E_all_40": (("megacap_tech", ALL, 0.40),),
}


def _year_candidates(dataset, strategy, year, params):
    signals, execution, earnings = dataset.inputs()
    start = pd.Timestamp(date(year, 1, 1))
    end = pd.Timestamp(date(year, 12, 31)) + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
    candidates = []
    for ticker in strategy_universe(strategy, TICKERS):
        frame = signals.get((ticker, strategy.timeframe))
        if frame is None or frame.empty:
            continue
        candidates.extend(collect_backtest_candidates(
            frame, ticker, start, end, params, strategy,
            execution_bars=execution[ticker],
            **({"earnings_store": earnings} if earnings is not None else {}),
        ))
    return candidates, execution


def run(years):
    from backtest_2025 import stats_from_portfolio

    strategy = REGISTRY[StrategyType.ENSEMBLE.value]
    base = replace(PARAMS, initial_backtest_equity=START_EQUITY)
    trades = {label: [] for label in PANEL}
    yearly = {label: {} for label in PANEL}
    for year in years:
        dataset = load_dataset(StrategyType.ENSEMBLE, year)
        candidates, execution = _year_candidates(dataset, strategy, year, base)
        for label, groups in PANEL.items():
            params = replace(base, exposure_groups=groups)
            result = run_configured_portfolio(list(candidates), price_frames=execution,
                                              params=params)
            stats = stats_from_portfolio(result)
            trades[label].extend(result.trades)
            yearly[label][year] = dict(
                trades=stats["trades"], pnl=round(stats["total_pnl"], 2),
                max_dd=round(stats["max_drawdown_pct"], 4),
                return_pct=round(result.return_pct, 4),
            )
            log.info("%s %s: %d trades, P&L %+.2f, max DD %.2f%%", year, label,
                     stats["trades"], stats["total_pnl"], stats["max_drawdown_pct"] * 100)

    def total(label):
        return sum(v["pnl"] for v in yearly[label].values())

    def mean_dd(label):
        values = [v["max_dd"] for v in yearly[label].values()]
        return sum(values) / len(values) if values else 0.0

    def ratio(label):
        return total(label) / mean_dd(label) if mean_dd(label) > 0 else float("inf")

    verdicts = {}
    for label in PANEL:
        if label == "A_none":
            continue
        lower_dd = sum(yearly[label][y]["max_dd"] < yearly["A_none"][y]["max_dd"] for y in years)
        keeps_pnl = total(label) >= 0.75 * total("A_none")
        better_ratio = ratio(label) > ratio("A_none")
        verdicts[label] = dict(
            years_with_lower_dd=lower_dd, pnl_share_of_A=round(total(label) / total("A_none"), 3)
            if total("A_none") else None,
            return_to_dd=round(ratio(label), 1), passes=bool(
                lower_dd >= min(4, len(years)) and keeps_pnl and better_ratio),
        )
    configs = {label: returns_from_trades(t) for label, t in trades.items()}
    winner = max(PANEL, key=ratio)
    report, bhy = evaluate_search(configs, winner=winner)
    return dict(years=list(years), start_equity=START_EQUITY, yearly=yearly,
                return_to_dd_A=round(ratio("A_none"), 1), verdicts=verdicts,
                winner_by_return_to_dd=winner, bhy_p=bhy, report=report.as_dict(),
                summary=report.summary())


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", nargs="+", type=int, default=[2022, 2023, 2024, 2025, 2026])
    parser.add_argument("--no-log", action="store_true", help="skip log_experiment")
    args = parser.parse_args(argv)
    out = run(args.years)
    REPORT_JSON.parent.mkdir(exist_ok=True)
    REPORT_JSON.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    log.info("Yearly results:\n%s", json.dumps(out["yearly"], indent=2))
    log.info("Verdicts vs A_none:\n%s", json.dumps(out["verdicts"], indent=2))
    log.info("Search-corrected report for %s:\n%s", out["winner_by_return_to_dd"], out["summary"])

    if not args.no_log:
        from dashboard import db as db_mod
        passing = [label for label, v in out["verdicts"].items() if v["passes"]]
        best = max(passing, key=lambda l: out["verdicts"][l]["return_to_dd"]) if passing else None
        pnl = lambda label, year: out["yearly"][label].get(year, {}).get("pnl", 0.0)
        label = best or "A_none"
        db_mod.log_experiment(
            "Correlated-group exposure cap (R04): A none / B semis 40% / C semis 20% / "
            "D all-five 60% / E all-five 40%, 5 predeclared variants, $100k start",
            f"exposure_groups={dict((k, v) for k, v in PANEL.items())[label]}; years={out['years']}",
            "ensemble", pnl(label, 2025), pnl(label, 2026),
            verdict=(f"PASSES drawdown rule: {best}" if best
                     else "REJECTED — no cap met the predeclared drawdown rule; keep none"),
            evidence=out["report"],
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
