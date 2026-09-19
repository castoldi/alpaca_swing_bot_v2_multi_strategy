"""Test non-price external risk-off signals as entry gates on the bot's own trades.

Pre-registered variants (fixed before any result was seen):
  vix_ts    : VIX / VIX3M > 1.0 (term-structure backwardation)
  credit    : HYG/IEF ratio < its 200-day SMA
  breadth   : fewer than 4 of 9 SPDR sectors above their 200-day SMA
  combo2of3 : at least two of the above
Results (docs/bear-market-playbook.md §2) were produced on commit d70a195,
whose backtest harness matches the Aug-24 study; run it from a worktree of
that commit, since later execution-model changes need pre-2016 1-minute data.

Point-in-time: flags[i] uses the close of days[i]; MarketRegimeGate.blocked only
reads bars that closed strictly before the entry's calendar date.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research"))

from config import PARAMS  # noqa: E402
from market_regime import MarketRegimeGate  # noqa: E402
from backtest_portfolio import run_annual_portfolio  # noqa: E402
import bear_market_experiment as bme  # noqa: E402

SECTORS = ["XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY"]


def closes(tickers):
    df = yf.download(tickers, start="2014-06-01", end="2026-09-19", auto_adjust=True, progress=False)["Close"]
    return df.ffill()


def build_flags():
    px = closes(["^VIX", "^VIX3M", "HYG", "IEF"] + SECTORS).dropna(how="all")
    vix_ts = (px["^VIX"] / px["^VIX3M"]) > 1.0
    ratio = px["HYG"] / px["IEF"]
    credit = ratio < ratio.rolling(200).mean()
    above = sum((px[s] > px[s].rolling(200).mean()).astype(int) for s in SECTORS)
    breadth = above < 4
    combo = (vix_ts.astype(int) + credit.astype(int) + breadth.astype(int)) >= 2
    return {"vix_ts": vix_ts, "credit": credit, "breadth": breadth, "combo2of3": combo}


def to_gate(flag: pd.Series, name: str) -> MarketRegimeGate:
    flag = flag.dropna()
    flag = flag[flag.index >= "2016-01-01"]
    days = np.array(flag.index.values.astype("datetime64[D]"))
    return MarketRegimeGate(days=days, flags=flag.values.astype(bool), mode=name)


def run_with_gate(candidates, frames_by_strategy, gate):
    results = {}
    for strat, per_year in candidates.items():
        yearly = {}
        for year, cands in per_year.items():
            r = run_annual_portfolio(
                cands,
                initial_equity=PARAMS.initial_backtest_equity,
                position_fraction=PARAMS.position_size_pct,
                max_positions=PARAMS.max_concurrent_positions,
                price_frames=frames_by_strategy[strat],
                regime_gate=gate,
            )
            yearly[year] = sum(t.pnl_dollars for t in r.trades)
        results[strat] = yearly
    return results


def main():
    flags = build_flags()
    for k, f in flags.items():
        f = f[f.index >= "2016-01-01"]
        by_year = f.groupby(f.index.year).mean().round(2).to_dict()
        print(f"{k:10} on {f.mean():.1%} of days | by year {by_year}")

    ticker_data = bme.load_ticker_data()
    candidates, frames = bme.collect_all_candidates(ticker_data)
    base = bme.score_variant(run_with_gate(candidates, frames, None))
    bme.print_block("baseline", base)
    rows = [("baseline", base)]
    for name, f in flags.items():
        scored = bme.score_variant(run_with_gate(candidates, frames, to_gate(f, name)))
        bme.print_block(name, scored, base)
        rows.append((name, scored))

    print("\nSUMMARY (all strategies summed, $1k/strategy/yr)")
    print(f"{'variant':12}{'2018':>9}{'2020':>9}{'2022':>9}{'2025':>9}{'bull':>10}{'total':>10}")
    for name, s in rows:
        py = s["per_year"]
        print(f"{name:12}{py[2018]:>+9.0f}{py[2020]:>+9.0f}{py[2022]:>+9.0f}{py[2025]:>+9.0f}"
              f"{s['bull_pnl']:>+10.0f}{s['total_pnl']:>+10.0f}")
    print("\nPER-STRATEGY 2022 / total")
    for strat in base["per_strategy"]:
        cells = "  ".join(
            f"{n}:{s['per_strategy'][strat][2022]:+.0f}/{sum(s['per_strategy'][strat].values()):+.0f}"
            for n, s in rows)
        print(f"{strat:15} {cells}")


if __name__ == "__main__":
    main()
