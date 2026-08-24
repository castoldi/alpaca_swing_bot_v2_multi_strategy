"""Where exactly does the bot lose money in 2022? Attribution by every axis."""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest_2025 import download_history  # noqa: E402
from backtest_portfolio import collect_backtest_candidates, run_annual_portfolio  # noqa: E402
from config import PARAMS, TICKERS  # noqa: E402
from strategies import get_enabled, strategy_universe  # noqa: E402

YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 2022


def main() -> None:
    strategies = get_enabled()
    timeframes = sorted({s.timeframe for s in strategies})
    needed = sorted({t for s in strategies for t in strategy_universe(s, TICKERS)})
    data = {}
    for tf in timeframes:
        for tk in needed:
            frame = download_history(tk, date(YEAR, 1, 1), date(YEAR, 12, 31), tf)
            data[(tk, tf)] = (
                frame if not frame.empty and len(frame) >= PARAMS.sma_slow + 5 else pd.DataFrame()
            )

    all_trades = []
    for strategy in strategies:
        frames = {
            tk: data[(tk, strategy.timeframe)]
            for tk in strategy_universe(strategy, TICKERS)
            if not data[(tk, strategy.timeframe)].empty
        }
        ws = pd.Timestamp(date(YEAR, 1, 1))
        we = pd.Timestamp(date(YEAR, 12, 31)) + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
        cands = []
        for tk, frame in frames.items():
            cands.extend(collect_backtest_candidates(frame, tk, ws, we, PARAMS, strategy))
        result = run_annual_portfolio(
            cands,
            initial_equity=PARAMS.initial_backtest_equity,
            position_fraction=PARAMS.position_size_pct,
            max_positions=PARAMS.max_concurrent_positions,
            price_frames=frames,
        )
        for t in result.trades:
            all_trades.append((strategy.name, t))

    print(f"\n{'=' * 70}\n{YEAR} LOSS ATTRIBUTION ({len(all_trades)} trades)\n{'=' * 70}")

    def bucket(keyfn, title):
        agg = defaultdict(lambda: [0, 0.0])
        for name, t in all_trades:
            k = keyfn(name, t)
            agg[k][0] += 1
            agg[k][1] += t.pnl_dollars
        print(f"\n--- by {title} ---")
        for k, (n, pnl) in sorted(agg.items(), key=lambda kv: kv[1][1]):
            print(f"  {str(k):<24} {n:>5} trades  {pnl:>+10.2f}")

    bucket(lambda n, t: n, "strategy")
    bucket(lambda n, t: t.ticker, "ticker")
    bucket(lambda n, t: t.exit_reason, "exit reason")
    bucket(lambda n, t: pd.Timestamp(t.entry_date).strftime("%Y-%m"), "entry month")

    print("\n--- 12 worst single trades ---")
    for name, t in sorted(all_trades, key=lambda x: x[1].pnl_dollars)[:12]:
        print(
            f"  {name:<16} {t.ticker:<5} in {pd.Timestamp(t.entry_date).date()} "
            f"out {pd.Timestamp(t.exit_date).date()} {t.exit_reason:<12} "
            f"{t.pnl_pct * 100:>+7.2f}%  ${t.pnl_dollars:>+8.2f}"
        )


if __name__ == "__main__":
    main()
