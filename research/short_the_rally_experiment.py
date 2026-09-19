"""Does shorting failed rallies in a confirmed bear market pay, and does it beat buying dips there?

Event study on daily bars, 1998-2026 (three real bears: 2000-02, 2007-09, 2022),
companion to docs/bear-market-playbook.md. Rules were fixed before any result
was seen and are exact mirrors of trend_pullback's shape, so long and short are
compared on equal terms:

  Regime  BEAR  = SPY close < SMA(200) AND SMA(200) below its value 20 days ago
                  (point-in-time: decided on day t's close, trade opens t+1)
  LONG    close > SMA(50), min RSI(14) over last 3 bars < 45, up day
  SHORT   close < SMA(50), max RSI(14) over last 3 bars > 55, down day
  Exit    SL 10% against, TP 2xATR(14) clipped to [3%, 8%], 5-day time stop.
          Both hit on one bar -> assume the stop (conservative). A gap through
          the stop fills at the open. 0.10% round-trip cost. One trade per
          ticker at a time.

Data: Yahoo daily (auto_adjust), because Alpaca stops at 2016. Daily bars are
coarser than the bot's 4h, so this answers "is there an edge here at all",
not "what would the bot have made".

Usage:  python research/short_the_rally_experiment.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import yfinance as yf

TICKERS = ["NVDA", "AMZN", "AMD", "META", "QQQ"]
START, END = "1997-01-01", "2026-09-19"
SL, TP_MULT, TP_MIN, TP_MAX, HOLD, COST = 0.10, 2.0, 0.03, 0.08, 5, 0.001
BEARS = {"2000-02 dot-com": ("2000-03-24", "2002-10-09"),
         "2007-09 GFC": ("2007-10-09", "2009-03-09"),
         "2022 rate hikes": ("2022-01-03", "2022-10-12")}


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df.Close.shift()
    tr = pd.concat([df.High - df.Low, (df.High - pc).abs(), (df.Low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def load(ticker: str) -> pd.DataFrame:
    df = yf.download(ticker, start=START, end=END, auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.dropna()


def bear_regime() -> pd.Series:
    spy = load("SPY").Close
    sma = spy.rolling(200).mean()
    return (spy < sma) & (sma < sma.shift(20))


def simulate(df: pd.DataFrame, side: str, regime: pd.Series, ticker: str) -> list[dict]:
    c = df.Close
    sma50, r, a = c.rolling(50).mean(), rsi(c), atr(df)
    if side == "long":
        sig = (c > sma50) & (r.rolling(3).min() < 45) & (c > c.shift())
    else:
        sig = (c < sma50) & (r.rolling(3).max() > 55) & (c < c.shift())
    reg = regime.reindex(df.index).fillna(False)
    trades, i, n = [], 60, len(df)
    O, H, L, C = df.Open.values, df.High.values, df.Low.values, c.values
    while i < n - 1:
        if not sig.iloc[i]:
            i += 1
            continue
        e = i + 1
        entry = O[e]
        tp_pct = float(np.clip(TP_MULT * a.iloc[i] / C[i], TP_MIN, TP_MAX))
        s = 1 if side == "long" else -1
        stop, target = entry * (1 - s * SL), entry * (1 + s * tp_pct)
        exit_px, reason, j = None, "time", e
        for j in range(e, min(e + HOLD, n)):
            gap_stop = (O[j] <= stop) if s == 1 else (O[j] >= stop)
            if j > e and gap_stop:
                exit_px, reason = O[j], "gap_stop"
                break
            hit_stop = (L[j] <= stop) if s == 1 else (H[j] >= stop)
            hit_tp = (H[j] >= target) if s == 1 else (L[j] <= target)
            if hit_stop:
                exit_px, reason = stop, "stop"
                break
            if hit_tp:
                exit_px, reason = target, "tp"
                break
        if exit_px is None:
            exit_px = C[j]
        ret = s * (exit_px / entry - 1) - COST
        trades.append(dict(ticker=ticker, side=side, date=df.index[e], ret=ret,
                           reason=reason, bear=bool(reg.iloc[i])))
        i = j + 1
    return trades


def stats(t: pd.DataFrame) -> dict:
    if t.empty:
        return dict(n=0)
    wins, losses = t.ret[t.ret > 0].sum(), -t.ret[t.ret <= 0].sum()
    return dict(n=len(t), win=f"{(t.ret > 0).mean():.0%}", avg=f"{t.ret.mean():+.2%}",
                sum=f"{t.ret.sum():+.0%}", pf=round(wins / losses, 2) if losses else np.inf,
                worst=f"{t.ret.min():+.1%}", stops=f"{t.reason.isin(['stop', 'gap_stop']).mean():.0%}")


def main() -> None:
    regime = bear_regime()
    print(f"BEAR regime on {regime.mean():.1%} of days since 1998")
    for name, (a, b) in BEARS.items():
        print(f"  {name}: regime on {regime[a:b].mean():.0%} of the bear")
    trades = []
    for tk in TICKERS:
        df = load(tk)
        for side in ("long", "short"):
            trades += simulate(df, side, regime, tk)
    t = pd.DataFrame(trades)
    t["year"] = t.date.dt.year

    print("\n== All trades by side x regime ==")
    rows = {f"{side:5} {'BEAR' if b else 'not-bear'}": stats(t[(t.side == side) & (t.bear == b)])
            for side in ("long", "short") for b in (True, False)}
    print(pd.DataFrame(rows).T.to_string())

    print("\n== Inside each real bear (peak -> trough), regime-gated ==")
    rows = {}
    for name, (a, b) in BEARS.items():
        w = t[(t.date >= a) & (t.date <= b) & t.bear]
        for side in ("long", "short"):
            rows[f"{name:16} {side}"] = stats(w[w.side == side])
    print(pd.DataFrame(rows).T.to_string())

    print("\n== SHORT in BEAR regime, per year ==")
    sb = t[(t.side == "short") & t.bear]
    print(sb.groupby("year").apply(lambda g: pd.Series(stats(g))).to_string())

    print("\n== SHORT in BEAR regime, per ticker ==")
    print(sb.groupby("ticker").apply(lambda g: pd.Series(stats(g))).to_string())

    print("\n== 8 worst short trades in BEAR regime (squeezes) ==")
    print(sb.nsmallest(8, "ret")[["ticker", "date", "ret", "reason"]].to_string(index=False))


if __name__ == "__main__":
    main()
