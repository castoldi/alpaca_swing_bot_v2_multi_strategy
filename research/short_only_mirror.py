"""What if the bot only ever shorted? Mirror-world backtest of the real strategy code.

Every price is inverted (P' = K / P; high' = K / low, low' = K / high), so an
uptrend becomes a downtrend and every indicator, vote and bracket in
``strategies/`` fires on the mirror image exactly as it would on a real chart.
A long trade on the mirror is a short trade on the real ticker at the same
timestamps. Its P&L is recomputed on real prices:

    short return = 1 - exit_real / entry_real = 1 - entry' / exit'

(the mirror's own long return, entry'/exit' ... inverted, overstates short gains
and understates short losses, so it is never used for P&L).

Geometry of the mirrored bracket on the real ticker: a 9% stop on the mirror is
a stop 9.9% *above* entry; a 5% target is a 4.8% decline. Close enough to "the
same bot, pointed down".

Both sides run through the same path — legacy 4h-candle execution (next-bar
open fill, no minute bars; the mirror has none), tax guard on, daily-loss kill
switch off, $1M notional start so whole-share rounding never skips a name, no
transaction or borrow costs. Annual return = sum of the year's trade P&L / $1M
(same 20%-per-slot sizing, not compounded).

Data: Alpaca SIP 4h, NVDA AMZN META AMD ARM, 2016-01 → 2026-09-09 (the whole
universe), plus NVDA alone on IBKR 4h from 2004 (the only name imported pre-2016).

Usage:  python research/short_only_mirror.py
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import data_feed  # noqa: E402
from backtest_portfolio import collect_backtest_candidates, run_annual_portfolio  # noqa: E402
from config import PARAMS, TICKERS  # noqa: E402
from market_cache import MarketDataCache  # noqa: E402
from research.significance import evaluate  # noqa: E402
from strategies import REGISTRY  # noqa: E402

EQUITY = 1_000_000.0
STRATEGIES = ["ensemble", "regime", "trend_pullback", "breakout", "mean_reversion", "momentum_macd"]
SIP_END = pd.Timestamp("2026-09-10")
CACHE = MarketDataCache()


def load(ticker: str, feed: str, start: str, end: pd.Timestamp) -> pd.DataFrame:
    bars = CACHE.get_bars(ticker, pd.Timestamp(start), end, "4h", feed=feed)
    bars = data_feed.completed_bars(bars, "4h")
    bars.attrs.update(timeframe="4h")
    return bars


def mirror(frame: pd.DataFrame) -> pd.DataFrame:
    k = float(frame["close"].median()) ** 2
    out = pd.DataFrame({
        "open": k / frame["open"], "high": k / frame["low"],
        "low": k / frame["high"], "close": k / frame["close"],
        "volume": frame["volume"],
    }, index=frame.index)
    out.attrs.update(frame.attrs)
    return out


def run(frames: dict[str, pd.DataFrame], strategy, years, short: bool):
    """Return {year: [(entry_date, real_return, real_pnl), ...]}."""
    params = replace(PARAMS, initial_backtest_equity=EQUITY)
    out = {}
    for year in years:
        ws = pd.Timestamp(f"{year}-01-01")
        we = pd.Timestamp(f"{year}-12-31 23:59:59")
        cands = []
        for ticker, frame in frames.items():
            # Indicators need ~60 bars of warmup; a 200-day lead is ample and
            # avoids recomputing the whole history for every year.
            frame = frame.loc[ws - pd.Timedelta(days=200): we]
            if len(frame) < 100:
                continue
            src = mirror(frame) if short else frame
            cands += collect_backtest_candidates(src, ticker, ws, we, params, strategy,
                                                 legacy_execution=True)
        result = run_annual_portfolio(
            cands, initial_equity=EQUITY, position_fraction=params.position_size_pct,
            max_positions=params.max_concurrent_positions, apply_kill_switch=False,
            params=params,
        )
        rows = []
        for t in result.trades:
            notional = t.shares * t.entry_price
            r = (1 - t.entry_price / t.exit_price) if short else t.pnl_pct
            rows.append((pd.Timestamp(t.entry_date), r, notional * r))
        out[year] = rows
    return out


def summarize(label, frames, years, strategies, trials):
    print(f"\n=== {label} ===")
    for name in strategies:
        strat = REGISTRY[name]
        res = {side: run(frames, strat, years, side == "short") for side in ("long", "short")}
        print(f"\n{name}")
        print(f"{'year':>6} {'long %':>8} {'short %':>8} {'sh trades':>9} {'sh win':>7}")
        for y in years:
            lp = sum(p for _, _, p in res["long"][y]) / EQUITY
            sr = res["short"][y]
            sp = sum(p for _, _, p in sr) / EQUITY
            win = np.mean([r > 0 for _, r, _ in sr]) if sr else float("nan")
            print(f"{y:>6} {lp*100:>+7.1f}% {sp*100:>+7.1f}% {len(sr):>9} {win*100:>6.0f}%")
        for side in ("long", "short"):
            allr = [r for y in years for _, r, _ in res[side][y]]
            tot = sum(p for y in years for _, _, p in res[side][y]) / EQUITY
            neg = sum(1 for y in years if sum(p for _, _, p in res[side][y]) < 0)
            rep = evaluate(allr, trials=trials) if len(allr) > 2 else None
            t = f"t={rep.t_stat:+.2f} adj-p={rep.adjusted_p_value:.3f}" if rep else ""
            print(f"  {side:>5}: total {tot*100:+.1f}% over {len(years)}y, "
                  f"{len(allr)} trades, avg {np.mean(allr)*100 if allr else 0:+.2f}%/trade, "
                  f"losing years {neg}/{len(years)}  {t}")


def main() -> int:
    global STRATEGIES
    if len(sys.argv) > 1:
        STRATEGIES = sys.argv[1:]
    trials = 12  # 6 strategies x long/short were planned; count them all
    sip = {t: load(t, "sip", "2015-06-01", SIP_END) for t in TICKERS}
    sip = {t: f for t, f in sip.items() if len(f) > 300}
    summarize("SIP 4h, 5 tickers, 2016-2026", sip, range(2016, 2027), STRATEGIES, trials)
    nvda = {"NVDA": load("NVDA", "ibkr", "2004-01-01", pd.Timestamp("2026-09-19"))}
    summarize("IBKR 4h, NVDA only, 2005-2026", nvda, range(2005, 2027), ["ensemble"], trials)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
