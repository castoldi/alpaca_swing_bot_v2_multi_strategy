"""Ensemble vote-gate ablation (V05 in docs/code-review-2026-09-21-verification.md).

Predeclared panel — every variant is reported and counted, losers included:

    A  live         current weights, score >= 0.30, >= 1 vote (live since v0.25.0)
    B  two_votes    current weights, score >= 0.30, >= 2 votes (the F16 rule)
    C  regime_only  only the Regime vote can open a trade (ensemble exits)
    D  equal        equal 0.20 weights, score >= 0.30 (i.e. >= 2 votes)

Every variant runs through the same annual runner the backtests use (minute
execution, daily-loss guard, sizing, archived + imported earnings), on one
frozen dataset per year. Promotion requires beating A in BOTH 2025 and 2026
AND clearing the search-corrected significance hurdle.

Usage:
    python research/ensemble_vote_ablation.py                  # 2022-2026
    python research/ensemble_vote_ablation.py --years 2025 2026
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import PARAMS, StrategyType  # noqa: E402
from logger_setup import get_logger  # noqa: E402
from research.optimizer import load_dataset  # noqa: E402
from research.significance import evaluate_search, returns_from_trades  # noqa: E402
from strategies.ensemble import EnsembleStrategy  # noqa: E402

log = get_logger(__name__)
REPORT_JSON = ROOT / "reports" / "ensemble_vote_ablation.json"


def variants():
    live = EnsembleStrategy()
    two = EnsembleStrategy()
    regime_only = EnsembleStrategy()
    regime_only.weights = {name: (w if name == "regime" else 0.0)
                           for name, w in regime_only.weights.items()}
    equal = EnsembleStrategy()
    equal.weights = {name: 0.20 for name in equal.weights}
    return {
        "A_live": (live, PARAMS),
        "B_two_votes": (two, replace(PARAMS, ensemble_min_votes=2)),
        "C_regime_only": (regime_only, PARAMS),
        "D_equal": (equal, PARAMS),
    }


def run(years):
    from backtest_2025 import run_strategy_year

    panel = variants()
    trades = {label: [] for label in panel}
    yearly = {label: {} for label in panel}
    fingerprints = {}
    for year in years:
        dataset = load_dataset(StrategyType.ENSEMBLE, year)
        fingerprints[year] = dataset.metadata.get("fingerprint")
        for label, (strategy, params) in panel.items():
            signals, execution, earnings = dataset.inputs()
            result, stats, _ = run_strategy_year(
                signals, strategy, year, params,
                execution_data=execution, earnings_store=earnings,
            )
            trades[label].extend(result.trades)
            yearly[label][year] = dict(
                trades=stats["trades"], pnl=round(stats["total_pnl"], 2),
                win_rate=round(stats["win_rate"], 3),
                max_dd=round(stats["max_drawdown_pct"], 4),
                return_pct=round(result.return_pct, 4),
            )
            log.info("%s %s: %d trades, P&L %+.2f", year, label,
                     stats["trades"], stats["total_pnl"])

    configs = {label: returns_from_trades(t) for label, t in trades.items()}
    challengers = [l for l in panel if l != "A_live"]
    winner = max(panel, key=lambda l: sum(v["pnl"] for v in yearly[l].values()))
    report, bhy = evaluate_search(configs, winner=winner)
    beats_live = {
        l: all(yearly[l].get(y, {}).get("pnl", float("-inf"))
               > yearly["A_live"].get(y, {}).get("pnl", float("inf"))
               for y in (2025, 2026) if y in years)
        for l in challengers
    }
    return dict(years=list(years), yearly=yearly, winner=winner,
                beats_live_2025_2026=beats_live,
                bhy_p=bhy, report=report.as_dict(), summary=report.summary(),
                fingerprints=fingerprints)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", nargs="+", type=int, default=[2022, 2023, 2024, 2025, 2026])
    parser.add_argument("--no-log", action="store_true", help="skip log_experiment")
    args = parser.parse_args(argv)
    out = run(args.years)
    REPORT_JSON.parent.mkdir(exist_ok=True)
    REPORT_JSON.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    log.info("Yearly results:\n%s", json.dumps(out["yearly"], indent=2))
    log.info("Beats live in 2025 and 2026: %s", out["beats_live_2025_2026"])
    log.info("Search-corrected verdict for %s:\n%s", out["winner"], out["summary"])

    if not args.no_log:
        from dashboard import db as db_mod
        keep = (out["winner"] != "A_live"
                and out["beats_live_2025_2026"].get(out["winner"])
                and out["report"].get("significant"))
        pnl = lambda label, year: out["yearly"][label].get(year, {}).get("pnl", 0.0)
        db_mod.log_experiment(
            "Ensemble vote-gate ablation (V05): A live 1-vote / B 2-vote / "
            "C regime-only / D equal weights, 4 predeclared variants",
            f"winner={out['winner']}; years={out['years']}",
            "ensemble", pnl(out["winner"], 2025), pnl(out["winner"], 2026),
            verdict=("KEPT " + out["winner"]) if keep else "REJECTED — keep A_live (min_votes=1)",
            evidence=out["report"],
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
