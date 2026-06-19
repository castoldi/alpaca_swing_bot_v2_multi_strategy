from __future__ import annotations
from typing import Optional
import numpy as np
import pandas as pd
from config import PARAMS, StrategyParams
from .base import BaseStrategy, EntrySignal


def _detect_regime(df: pd.DataFrame, idx: int, p: StrategyParams) -> str:
    row = df.iloc[idx]
    if pd.isna(row["ema_short"]) or pd.isna(row["ema_long"]):
        return "neutral"
    if row["ema_short"] > row["ema_long"] and row["close"] > row["sma_slow"]:
        return "risk_on"
    if row["ema_short"] < row["ema_long"] and row["close"] < row["sma_slow"]:
        return "risk_off"
    return "neutral"


class RegimeAdaptiveStrategy(BaseStrategy):
    name = "regime"
    label = "Regime Adaptive"
    version = "V2"
    color = "#fb923c"
    description = (
        "Detects market regime via EMA(10)/EMA(50) cross. "
        "Risk-on: buy dips in strong uptrends. "
        "Risk-off: defensive oversold bounces only. "
        "Neutral: standard trend-pullback style."
    )
    params_display = ["SL adaptive", "TP 2×ATR [3%–8%]", "Time stop 5d if breakeven+", "EMA(10/50)"]

    def check_entry(self, df: pd.DataFrame, idx: int, p: StrategyParams = PARAMS) -> Optional[EntrySignal]:
        if idx < max(p.sma_slow, p.rsi_period, p.atr_period, p.regime_lookback) + 2:
            return None
        row = df.iloc[idx]
        prev = df.iloc[idx - 1]
        if pd.isna(row["sma_slow"]) or pd.isna(row["atr"]) or pd.isna(row["ema_long"]):
            return None

        regime = _detect_regime(df, idx, p)

        if regime == "risk_on":
            if not (row["close"] > row["ema_short"] > row["ema_long"]):
                return None
            if not (row["rsi"] > 45 and row["close"] > prev["close"]):
                return None
            sl_mult = p.regime_risk_on_mult * p.stop_loss_pct
        elif regime == "risk_off":
            if not (row["close"] < row["ema_short"] and row["rsi"] < 40):
                return None
            if not (row["close"] > prev["close"]):
                return None
            sl_mult = p.stop_loss_pct
        else:
            if not (row["close"] > row["sma_slow"] and row["rsi"] > 40):
                return None
            if not (row["close"] > row["open"] and row["close"] > prev["close"]):
                return None
            sl_mult = p.stop_loss_pct

        entry = float(row["close"])
        sl = entry * (1.0 - sl_mult)
        tp_raw = entry + p.atr_tp_multiple * float(row["atr"])
        tp_pct = float(np.clip((tp_raw - entry) / entry, p.take_profit_floor_pct, p.take_profit_cap_pct))
        tp = entry * (1.0 + tp_pct)
        return EntrySignal(
            date=row.name if isinstance(row.name, pd.Timestamp) else pd.Timestamp(row.name),
            entry_price=entry, stop_loss=sl, take_profit=tp,
            atr=float(row["atr"]), rsi=float(row["rsi"]),
            strategy=f"regime_{regime}",
        )
