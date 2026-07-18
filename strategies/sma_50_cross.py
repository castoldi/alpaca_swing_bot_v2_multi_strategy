from __future__ import annotations
from typing import Optional

import numpy as np
import pandas as pd

from config import PARAMS, StrategyParams
from .base import BaseStrategy, EntrySignal


class SMA50CrossStrategy(BaseStrategy):
    name = "sma_50_cross"
    label = "SMA 50 Cross"
    version = "V2"
    color = "#38bdf8"
    timeframe = "1d"
    exit_mode = "signal_with_stop"
    has_take_profit = False
    description = (
        "Daily close crosses above SMA(50) to buy and crosses below to sell. "
        "A broker-held 10% emergency stop protects the position."
    )
    params_display = ["Daily", "SMA 50", "SL 10%", "Cross-down exit"]

    @staticmethod
    def _values(df: pd.DataFrame, idx: int, p: StrategyParams):
        if idx < p.sma_cross_period:
            return None
        prev, row = df.iloc[idx - 1], df.iloc[idx]
        values = (
            prev["close"], prev["sma_cross"], row["close"], row["sma_cross"],
        )
        return values if all(np.isfinite(float(value)) for value in values) else None

    def check_entry(
        self, df: pd.DataFrame, idx: int, p: StrategyParams = PARAMS
    ) -> Optional[EntrySignal]:
        values = self._values(df, idx, p)
        if values is None:
            return None
        prev_close, prev_sma, close, current_sma = values
        if not (prev_close <= prev_sma and close > current_sma):
            return None
        row = df.iloc[idx]
        entry = float(close)
        return EntrySignal(
            date=pd.Timestamp(row.name),
            entry_price=entry,
            stop_loss=entry * (1.0 - p.sma_cross_stop_loss_pct),
            take_profit=0.0,
            atr=float(row["atr"]),
            rsi=float(row["rsi"]),
            strategy=self.name,
        )

    def check_exit(
        self, df: pd.DataFrame, idx: int, p: StrategyParams = PARAMS
    ) -> Optional[str]:
        values = self._values(df, idx, p)
        if values is None:
            return None
        prev_close, prev_sma, close, current_sma = values
        if prev_close >= prev_sma and close < current_sma:
            return "sma_cross_down"
        return None
