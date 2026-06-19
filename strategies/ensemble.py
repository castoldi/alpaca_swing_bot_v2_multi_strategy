from __future__ import annotations
from typing import Optional
import numpy as np
import pandas as pd
from config import PARAMS, StrategyParams
from .base import BaseStrategy, EntrySignal
from .trend_pullback import TrendPullbackStrategy
from .breakout import BreakoutStrategy
from .mean_reversion import MeanReversionStrategy
from .momentum_macd import MomentumMACDStrategy
from .regime_adaptive import RegimeAdaptiveStrategy

# Weights tuned by autoresearch (2026-05-28): rebalanced based on actual P&L.
# Baseline: trend:0.30, breakout:0.25, mr:0.10, macd:0.20, regime:0.15 → 2025: -$28.15
# New: regime weighted highest (best performer), breakout/mr reduced.
ENSEMBLE_WEIGHTS = {
    "trend_pullback": 0.20,
    "breakout": 0.15,
    "mean_reversion": 0.05,
    "momentum_macd": 0.25,
    "regime": 0.35,
}


class EnsembleStrategy(BaseStrategy):
    name = "ensemble"
    label = "Ensemble"
    version = "V2"
    color = "#f472b6"
    description = (
        "Weighted vote of all 5 base strategies must score ≥ 0.30. "
        "Weights: Regime 35%, MACD 25%, Trend 20%, Breakout 15%, Mean Rev 5%. "
        "Enters only when multiple strategies agree."
    )
    params_display = ["SL 9%", "TP 2.5×ATR [4%–12%]", "Time stop 6d if breakeven+", "Score ≥ 0.30"]

    def __init__(self):
        super().__init__()
        self._members: dict[str, BaseStrategy] = {
            "trend_pullback": TrendPullbackStrategy(),
            "breakout": BreakoutStrategy(),
            "mean_reversion": MeanReversionStrategy(),
            "momentum_macd": MomentumMACDStrategy(),
            "regime": RegimeAdaptiveStrategy(),
        }

    def check_entry(self, df: pd.DataFrame, idx: int, p: StrategyParams = PARAMS) -> Optional[EntrySignal]:
        if idx < 60:
            return None
        row = df.iloc[idx]
        if pd.isna(row.get("atr", float("nan"))) or pd.isna(row.get("rsi", float("nan"))):
            return None

        signals = {name: s.check_entry(df, idx, p) for name, s in self._members.items()}
        score = sum(ENSEMBLE_WEIGHTS.get(name, 0) for name, sig in signals.items() if sig is not None)

        if score < 0.30:
            return None

        entry = float(row["close"])
        sl = entry * (1.0 - p.ensemble_stop_loss_pct)
        tp_raw = entry + p.ensemble_tp_multiple * float(row["atr"])
        tp_pct = float(np.clip((tp_raw - entry) / entry, p.ensemble_tp_floor_pct, p.ensemble_tp_cap_pct))
        tp = entry * (1.0 + tp_pct)
        return EntrySignal(
            date=row.name if isinstance(row.name, pd.Timestamp) else pd.Timestamp(row.name),
            entry_price=entry, stop_loss=sl, take_profit=tp,
            atr=float(row["atr"]), rsi=float(row["rsi"]),
            strategy=f"ensemble_{score:.2f}",
        )
