"""Does starting capital change the results? Live-readiness Phase 2 / 3.

Every tuned number in this project came from $1,000 annual backtests, where a
20% slot is $200 and whole-share sizing skips most of the universe. The user's
planned live start is ~$5,000. Candidates (signals + exits) do not depend on
capital, so they are collected once per strategy and year on the production
engine (the yearly runners' path: minute execution, earnings filter, kill
switch) and replayed through the annual portfolio at each capital level.

Combinations are run two ways at the combined capital:
  shared  - one book: all strategies' candidates compete for the same 5 slots,
            20% sizing and leveraged cap (what one multi-strategy process does)
  sleeves - each strategy gets capital / N as its own book, summed (whole-share
            fragmentation included — the earlier combination table ignored it)

Candidates are cached (pickle) in --cache-dir so an interrupted run resumes.

Usage:
  python research/capital_size_experiment.py                         # ensemble, $1k/$5k/$100k
  python research/capital_size_experiment.py --strategies all --capitals 1000 5000 \
      --combos ensemble+tqqq_momentum breakout+tqqq_momentum
"""
from __future__ import annotations

import argparse
import pickle
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
from strategies import REGISTRY, get_enabled, strategy_universe  # noqa: E402

YEARS = range(2022, 2027)
_FRAMES: dict[tuple[str, str], pd.DataFrame] = {}


def frame(ticker: str, timeframe: str) -> pd.DataFrame:
    key = (ticker, timeframe)
    if key not in _FRAMES:
        f = download_history(ticker, date(YEARS[0], 1, 1), date.today(), timeframe)
        _FRAMES[key] = f if (not f.empty and len(f) >= PARAMS.sma_slow + 5) else pd.DataFrame()
    return _FRAMES[key]


def window(year: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(date(year, 1, 1))
    return start, start + pd.DateOffset(years=1) - pd.Timedelta(nanoseconds=1)


def active_frames(name: str, year: int) -> dict[str, pd.DataFrame]:
    """Tickers with bars this year (ARM listed 2023-09)."""
    strategy = REGISTRY[name]
    start, end = window(year)
    out = {}
    for ticker in strategy_universe(strategy, TICKERS):
        f = frame(ticker, strategy.timeframe)
        if not f.empty and ((f.index >= start) & (f.index <= end)).any():
            out[ticker] = f
    return out


def candidates(name: str, year: int, cache_dir: Path) -> list:
    path = cache_dir / f"{name}_{year}.pkl"
    if path.exists():
        return pickle.loads(path.read_bytes())
    start, end = window(year)
    found = []
    for ticker, f in active_frames(name, year).items():
        found += collect_backtest_candidates(f, ticker, start, end, PARAMS, REGISTRY[name])
    path.write_bytes(pickle.dumps(found))
    print(f"  {name} {year}: {len(found)} candidates", flush=True)
    return found


def run_book(cands: list, capital: float, price_frames: dict) -> tuple[float, float, int, list]:
    """(P&L $, realized max DD, trades, trades list) for one annual book."""
    if not cands:
        return 0.0, 0.0, 0, []
    params = replace(PARAMS, initial_backtest_equity=capital)
    result = run_configured_portfolio(cands, params=params, price_frames=price_frames)
    trades = list(result.trades)
    return result.ending_equity - capital, compute_max_drawdown(trades, capital), len(trades), trades


def summarize(label: str, capital: float, per_year: dict[int, tuple[float, float, int]]) -> dict:
    comp, worst, dd = 1.0, float("inf"), 0.0
    for pnl, d, _ in per_year.values():
        comp *= 1 + pnl / capital
        worst = min(worst, pnl / capital)
        dd = max(dd, d)
    cells = "".join(f"{p / capital * 100:>+8.1f}%" for p, _, _ in per_year.values())
    trades = sum(n for _, _, n in per_year.values())
    print(f"{label:<44}{capital:>8,.0f}{cells}{(comp - 1) * 100:>+9.1f}%{worst * 100:>+8.1f}%"
          f"{dd * 100:>7.1f}%{trades:>7}", flush=True)
    return dict(label=label, capital=capital, compounded=comp - 1, worst=worst, dd=dd)


def header() -> None:
    years = "".join(f"{y:>9}" for y in YEARS)
    print(f"\n{'portfolio':<44}{'capital':>8}{years}{'5y comp':>10}{'worst':>8}{'maxDD':>7}{'trades':>7}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strategies", nargs="+", default=["ensemble"],
                        help="names, or 'all' for every enabled strategy")
    parser.add_argument("--capitals", nargs="+", type=float, default=[1000, 5000, 100000])
    parser.add_argument("--combos", nargs="*", default=[],
                        help="e.g. ensemble+tqqq_momentum; run at the largest capital")
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "cache" / "capital_experiment")
    args = parser.parse_args()
    names = [s.name for s in get_enabled()] if args.strategies == ["all"] else args.strategies
    args.cache_dir.mkdir(parents=True, exist_ok=True)

    results = []
    header()
    for name in names:
        for cap in args.capitals:
            per_year = {}
            for year in YEARS:
                pnl, dd, n, _ = run_book(candidates(name, year, args.cache_dir), cap,
                                         active_frames(name, year))
                per_year[year] = (pnl, dd, n)
            results.append(summarize(name, cap, per_year))

    cap = max(args.capitals)
    for combo in args.combos:
        members = combo.split("+")
        shared, sleeves = {}, {}
        for year in YEARS:
            cands = [c for m in members for c in candidates(m, year, args.cache_dir)]
            frames = {}
            for m in members:
                for t, f in active_frames(m, year).items():
                    frames.setdefault(t, f)
            pnl, dd, n, _ = run_book(cands, cap, frames)
            shared[year] = (pnl, dd, n)
            parts = [run_book(candidates(m, year, args.cache_dir), cap / len(members),
                              active_frames(m, year)) for m in members]
            # Sleeve drawdowns are not additive; report the worst sleeve's as a floor.
            sleeves[year] = (sum(p[0] for p in parts), max(p[1] for p in parts) / len(members),
                             sum(p[2] for p in parts))
        results.append(summarize(f"{combo} (shared book)", cap, shared))
        results.append(summarize(f"{combo} (sleeves {cap / len(members):,.0f} each)", cap, sleeves))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
