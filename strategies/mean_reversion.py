from __future__ import annotations
from typing import Optional
import numpy as np
import pandas as pd
from config import PARAMS, StrategyParams
from .base import BaseStrategy, EntrySignal


class MeanReversionStrategy(BaseStrategy):
    name = "mean_reversion"
    label = "Mean Reversion"
    version = "V1"
    color = "#a78bfa"
    description = (
        "Price above SMA(50) but below SMA(20) (pullback). RSI pulled back below 50. "
        "Price near or below Bollinger lower band (2.2σ). "
        "Bounce confirmed by close > prev close."
    )
    params_display = ["SL 7%", "TP 1.5×ATR [1.5%–5%]", "Time stop 3d if breakeven+", "Bollinger 2.2σ"]

    def check_entry(self, df: pd.DataFrame, idx: int, p: StrategyParams = PARAMS) -> Optional[EntrySignal]:
        if idx < max(p.sma_slow, p.rsi_period, p.atr_period, p.mr_sma_fast) + 1:
            return None
        row = df.iloc[idx]
        prev = df.iloc[idx - 1]
        if pd.isna(row["sma_slow"]) or pd.isna(row["atr"]) or pd.isna(row["bb_lower"]):
            return None
        if not (row["close"] > row["sma_slow"]):
            return None
        rsi_window = df["rsi"].iloc[max(0, idx - p.mr_rsi_lookback + 1): idx + 1]
        if rsi_window.min() > p.mr_rsi_oversold:
            return None
        if pd.isna(row["sma_fast"]):
            return None
        if row["close"] > row["sma_fast"] * (1.0 - p.mr_deviation_pct):
            return None
        if not (row["close"] > prev["close"]):
            return None
        entry = float(row["close"])
        sl = entry * (1.0 - p.mr_stop_loss_pct)
        tp_raw = entry + p.mr_atr_multiple * float(row["atr"])
        tp_pct = float(np.clip((tp_raw - entry) / entry, p.mr_tp_floor_pct, p.mr_tp_cap_pct))
        tp = entry * (1.0 + tp_pct)
        return EntrySignal(
            date=row.name if isinstance(row.name, pd.Timestamp) else pd.Timestamp(row.name),
            entry_price=entry, stop_loss=sl, take_profit=tp,
            atr=float(row["atr"]), rsi=float(row["rsi"]),
            strategy=self.name,
        )
