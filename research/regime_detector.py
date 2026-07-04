"""Market regime detector — classifies trends for adaptive strategy."""
from __future__ import annotations

import numpy as np
import pandas as pd


def detect_trend_regime(
    df: pd.DataFrame,
    sma_short: int = 10,
    sma_long: int = 50,
) -> pd.Series:
    """Classify each bar as 'bull', 'bear', or 'neutral' based on EMA cross."""
    short_ema = df["close"].ewm(span=sma_short, adjust=False).mean()
    long_ema = df["close"].ewm(span=sma_long, adjust=False).mean()

    regime = pd.Series("neutral", index=df.index)
    regime.loc[short_ema > long_ema] = "bull"
    regime.loc[short_ema < long_ema] = "bear"
    return regime


def detect_volatility_regime(
    df: pd.DataFrame,
    atr_period: int = 14,
    lookback: int = 60,
) -> pd.Series:
    """Classify volatility as 'low', 'normal', or 'high'."""
    from strategy import atr as calc_atr
    atr_vals = calc_atr(df["high"], df["low"], df["close"], atr_period)
    atr_pct = atr_vals / df["close"] * 100

    regime = pd.Series("normal", index=df.index)
    rolling_mean = atr_pct.rolling(lookback, min_periods=lookback).mean()
    rolling_std = atr_pct.rolling(lookback, min_periods=lookback).std()

    regime.loc[atr_pct < rolling_mean - rolling_std] = "low"
    regime.loc[atr_pct > rolling_mean + rolling_std] = "high"
    return regime


def combine_regimes(
    df: pd.DataFrame,
    sma_short: int = 10,
    sma_long: int = 50,
    atr_period: int = 14,
) -> pd.DataFrame:
    """Return a DataFrame with regime classifications and a composite score."""
    trend = detect_trend_regime(df, sma_short, sma_long)
    vol = detect_volatility_regime(df, atr_period)

    out = pd.DataFrame(index=df.index)
    out["trend_regime"] = trend
    out["vol_regime"] = vol

    # Composite: +1 bull, -1 bear, 0 neutral
    out["trend_score"] = np.where(trend == "bull", 1, np.where(trend == "bear", -1, 0))
    out["vol_score"] = np.where(vol == "low", 1, np.where(vol == "high", -1, 0))
    out["composite"] = out["trend_score"] + out["vol_score"]

    return out