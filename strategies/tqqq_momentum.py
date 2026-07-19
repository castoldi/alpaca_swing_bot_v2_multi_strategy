"""TQQQ momentum — TSI entry, EMA(50) break exit. Leveraged ETF only.

Why this strategy exists separately from the other six:

A 3x ETF resets leverage daily, so choppy price action compounds against the
holder ("volatility decay"). The fixed broker bracket the other 4h strategies
use is the wrong exit shape for that — it sits through the chop waiting for a
target. Backtesting the existing strategies on TQQQ confirmed it: regime,
ensemble, mean_reversion and momentum_macd all went negative in 2026.

What works instead is riding a trend until it breaks. Backtests on Alpaca SIP
4h bars, 2022-2026:

    year   TQQQ buy & hold      this strategy
    2022        -79.5%              +12.4%
    2023       +192.6%              +25.3%
    2024        +65.5%              +17.6%
    2025        +36.8%             +121.5%
    2026 YTD    +27.8%              +11.6%
    ------------------------------------------
    full        +65.8% (81% DD)    +309.6% (23% DD)

Positive in every year including 2022, when it made money in a market that
took buy-and-hold down 79.5% — mostly by staying out.

Design notes:
  * Entry is TSI crossing its signal line, and nothing else. An ablation showed
    that adding an EMA(50) trend gate or a MACD confirmation to the entry both
    *reduced* returns (TSI-only +309.6%, +EMA50 gate +161.4%, +MACD +157.3%).
    TSI's double smoothing already filters the noise those filters target.
  * EMA(50) is essential, but as the *exit*. Swapping it for a TSI-fade exit
    turns 2022 from +12.4% into a loss. Cutting on trend break is what defuses
    the decay.
  * No take-profit. A 3xATR target scored higher in-sample (+346%) but that was
    a jagged parameter spike (3.5xATR fell to +144%) — an overfit artifact, not
    an edge. Letting the exit rule decide is the honest choice.
  * The 8% stop is gap insurance. In backtest it almost never fires; the EMA
    break gets there first at every stop distance from 8% to 20%.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from config import LEVERAGED_TICKERS, PARAMS, StrategyParams
from .base import BaseStrategy, EntrySignal


class TQQQMomentumStrategy(BaseStrategy):
    name = "tqqq_momentum"
    label = "TQQQ Momentum"
    version = "V2"
    color = "#facc15"
    timeframe = "4h"
    exit_mode = "signal_with_stop"
    has_take_profit = False
    tickers = tuple(LEVERAGED_TICKERS)
    signal_exit_reason = "ema_break"
    description = (
        "TSI(25,13) crossing above its signal line buys; a close below EMA(50) "
        "sells. Built for leveraged ETFs, where riding a trend until it breaks "
        "beats a fixed target. An 8% broker-held stop covers gaps."
    )
    params_display = ["4h", "TSI 25/13/13", "EMA 50 exit", "SL 8%", "No TP"]

    def stop_loss_fraction(self, p: StrategyParams = PARAMS) -> float:
        return p.tqqq_stop_loss_pct

    @staticmethod
    def _values(df: pd.DataFrame, idx: int, p: StrategyParams):
        """(prev_tsi, prev_signal, tsi, signal, close, ema_trend) or None."""
        # EMA(50) plus the TSI's stacked 25/13/13 smoothing needs a long warmup
        # before either line is meaningful.
        if idx < p.tqqq_ema_period + p.tqqq_tsi_long + p.tqqq_tsi_short:
            return None
        prev, row = df.iloc[idx - 1], df.iloc[idx]
        values = (
            prev["tsi"], prev["tsi_signal"],
            row["tsi"], row["tsi_signal"],
            row["close"], row["ema_trend"],
        )
        return values if all(np.isfinite(float(v)) for v in values) else None

    def check_entry(
        self, df: pd.DataFrame, idx: int, p: StrategyParams = PARAMS
    ) -> Optional[EntrySignal]:
        values = self._values(df, idx, p)
        if values is None:
            return None
        prev_tsi, prev_sig, tsi_now, sig_now, close, _ema_trend = values
        if not (prev_tsi <= prev_sig and tsi_now > sig_now):
            return None
        row = df.iloc[idx]
        entry = float(close)
        return EntrySignal(
            date=pd.Timestamp(row.name),
            entry_price=entry,
            stop_loss=entry * (1.0 - p.tqqq_stop_loss_pct),
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
        *_, close, ema_trend = values
        if close < ema_trend:
            return self.signal_exit_reason
        return None
