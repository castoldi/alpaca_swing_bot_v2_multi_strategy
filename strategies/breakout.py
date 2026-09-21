from __future__ import annotations
from typing import Optional
import numpy as np
import pandas as pd
from config import PARAMS, StrategyParams
from .base import BaseStrategy, EntrySignal


class BreakoutStrategy(BaseStrategy):
    name = "breakout"
    label = "Breakout"
    version = "V1"
    color = "#f59e0b"
    description = (
        "Intrabar high breaks above the prior 20-bar high with ≥1.5× average volume. "
        "Price above SMA(50). RSI at least 50 and rising. "
        "Current range must be ≤1.3× the prior 10-bar average range."
    )
    params_display = ["SL 8%", "TP 3×ATR [5%–15%]", "Time stop 7 sessions if breakeven+", "Volume 1.5×"]

    def check_entry(self, df: pd.DataFrame, idx: int, p: StrategyParams = PARAMS) -> Optional[EntrySignal]:
        if idx < max(p.sma_slow, p.rsi_period, p.atr_period, p.breakout_lookback) + 1:
            return None
        row = df.iloc[idx]
        if pd.isna(row["sma_slow"]) or pd.isna(row["atr"]) or pd.isna(row["sma_vol"]):
            return None
        if not (row["close"] > row["sma_slow"]):
            return None
        prior = df.iloc[idx - p.breakout_range_lookback: idx]
        prior_ranges = prior["high"] - prior["low"]
        current_range = float(row["high"] - row["low"])
        if (not np.isfinite(current_range)
                or not np.isfinite(prior_ranges.to_numpy()).all()):
            return None
        avg_range = float(prior_ranges.mean())
        if current_range > avg_range * p.breakout_range_mult:
            return None
        if pd.isna(row["sma_vol"]) or row["volume"] < row["sma_vol"] * p.breakout_volume_mult:
            return None
        high_window = df["high"].iloc[max(0, idx - p.breakout_lookback): idx]
        prior_high = high_window.max()
        if pd.isna(prior_high) or not (float(row["high"]) > float(prior_high)):
            return None
        prev = df.iloc[idx - 1]
        # Positive evidence is required: NaN must not pass either comparison.
        if not (row["rsi"] >= 50 and row["rsi"] > prev["rsi"]):
            return None
        entry = float(row["close"])
        sl = entry * (1.0 - p.breakout_stop_loss_pct)
        tp_raw = entry + p.breakout_atr_multiple * float(row["atr"])
        tp_pct = float(np.clip((tp_raw - entry) / entry, p.breakout_tp_floor_pct, p.breakout_tp_cap_pct))
        tp = entry * (1.0 + tp_pct)
        return EntrySignal(
            date=row.name if isinstance(row.name, pd.Timestamp) else pd.Timestamp(row.name),
            entry_price=entry, stop_loss=sl, take_profit=tp,
            atr=float(row["atr"]), rsi=float(row["rsi"]),
            strategy=self.name,
        )
