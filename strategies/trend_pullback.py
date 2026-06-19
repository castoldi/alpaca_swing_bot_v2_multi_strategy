from __future__ import annotations
from typing import Optional
import numpy as np
import pandas as pd
from config import PARAMS, StrategyParams
from .base import BaseStrategy, EntrySignal


class TrendPullbackStrategy(BaseStrategy):
    name = "trend_pullback"
    label = "Trend Pullback"
    version = "V1"
    color = "#60a5fa"
    description = (
        "Price above SMA(50). RSI dipped below 55 in recent bars, then a bounce bar "
        "(close > open, close > prev close, rising RSI). Skips entries 3 days before "
        "earnings to avoid gap risk."
    )
    params_display = ["SL 10%", "TP 2×ATR [3%–8%]", "Time stop 5d if breakeven+", "Earnings filter"]

    def check_entry(self, df: pd.DataFrame, idx: int, p: StrategyParams = PARAMS) -> Optional[EntrySignal]:
        if idx < max(p.sma_slow, p.rsi_period, p.atr_period) + 1:
            return None
        row = df.iloc[idx]
        prev = df.iloc[idx - 1]
        if pd.isna(row["sma_slow"]) or pd.isna(row["atr"]):
            return None
        if "near_earnings" in df.columns and row["near_earnings"]:
            return None
        if not (row["close"] > row["sma_slow"]):
            return None
        rsi_window = df["rsi"].iloc[max(0, idx - p.rsi_lookback + 1): idx + 1]
        if rsi_window.min() > p.rsi_pullback_max:
            return None
        if not (row["close"] > row["open"] and row["close"] > prev["close"]):
            return None
        if not (row["rsi"] > prev["rsi"]):
            return None
        entry = float(row["close"])
        sl = entry * (1.0 - p.stop_loss_pct)
        tp_raw = entry + p.atr_tp_multiple * float(row["atr"])
        tp_pct = float(np.clip((tp_raw - entry) / entry, p.take_profit_floor_pct, p.take_profit_cap_pct))
        tp = entry * (1.0 + tp_pct)
        return EntrySignal(
            date=row.name if isinstance(row.name, pd.Timestamp) else pd.Timestamp(row.name),
            entry_price=entry, stop_loss=sl, take_profit=tp,
            atr=float(row["atr"]), rsi=float(row["rsi"]),
            strategy=self.name,
        )
