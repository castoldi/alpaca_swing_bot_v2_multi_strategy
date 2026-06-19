from __future__ import annotations
from typing import Optional
import numpy as np
import pandas as pd
from config import PARAMS, StrategyParams
from .base import BaseStrategy, EntrySignal


class MomentumMACDStrategy(BaseStrategy):
    name = "momentum_macd"
    label = "MACD Momentum"
    version = "V2"
    color = "#34d399"
    description = (
        "MACD histogram just crossed above zero (fresh bullish cross). "
        "RSI above 50 and rising. Price above both SMA(20) and SMA(50). "
        "Not on very low volume (<0.7× avg)."
    )
    params_display = ["SL 9%", "TP 2.5×ATR [4%–12%]", "Time stop 6d if breakeven+", "MACD(12,26,9)"]

    def check_entry(self, df: pd.DataFrame, idx: int, p: StrategyParams = PARAMS) -> Optional[EntrySignal]:
        if idx < max(p.sma_slow, p.rsi_period, p.atr_period, p.macd_slow) + 3:
            return None
        row = df.iloc[idx]
        prev = df.iloc[idx - 1]
        if pd.isna(row["sma_slow"]) or pd.isna(row["macd"]) or pd.isna(row["atr"]):
            return None
        if not (row["close"] > row["sma_slow"]):
            return None
        if not (row["macd_hist"] > 0 and prev["macd_hist"] <= 0):
            return None
        if not (row["rsi"] > 50 and row["rsi"] > prev["rsi"]):
            return None
        if pd.isna(row["sma_fast"]) or not (row["close"] > row["sma_fast"]):
            return None
        if not pd.isna(row["sma_vol"]) and row["volume"] < row["sma_vol"] * 0.7:
            return None
        entry = float(row["close"])
        sl = entry * (1.0 - p.macd_stop_loss_pct)
        tp_raw = entry + p.macd_tp_multiple * float(row["atr"])
        tp_pct = float(np.clip((tp_raw - entry) / entry, p.macd_tp_floor_pct, p.macd_tp_cap_pct))
        tp = entry * (1.0 + tp_pct)
        return EntrySignal(
            date=row.name if isinstance(row.name, pd.Timestamp) else pd.Timestamp(row.name),
            entry_price=entry, stop_loss=sl, take_profit=tp,
            atr=float(row["atr"]), rsi=float(row["rsi"]),
            strategy=self.name,
        )
