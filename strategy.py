"""Swing-trade strategy engine V2 — 6 strategies + indicators.

V1 strategies (kept for baseline):
  - trend_pullback, breakout, mean_reversion

V2 new strategies:
  - momentum_macd  (MACD cross + RSI momentum)
  - ensemble       (weighted vote of all strategies)
  - regime         (market-regime-aware adaptive strategy)

All strategies share the same exit framework (SL / TP / time stop).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from config import PARAMS, StrategyParams, StrategyType


# ── TP reachability filter ──────────────────────────────────────────────────

def is_tp_reachable_in_days(entry_price: float, take_profit: float, atr: float, days: int = 2) -> bool:
    """Check if the take-profit target is reachable within N trading days.

    Uses average daily range (ATR) as the estimate of achievable daily movement.
    A trade only opens if the ATR-based estimate suggests the TP can be reached
    within the specified days, preventing low-probability entries where the
    target is too far relative to normal daily volatility.
    """
    if atr <= 0 or entry_price <= 0:
        return False
    distance = take_profit - entry_price
    if distance <= 0:
        return True
    return distance <= days * atr


def split_take_profit(entry_price: float, take_profit: float) -> tuple[float, float, float]:
    """Place 3 TP levels at 1/3, 2/3, and full of the entry->target distance."""
    d = take_profit - entry_price
    return (entry_price + d / 3.0, entry_price + 2.0 * d / 3.0, take_profit)


def split_qty(qty: int) -> list[int]:
    """Whole-share split for 3 TP legs: floor thirds, remainder on the last leg.

    Returns [] when there are fewer than 3 shares (caller falls back to a single
    target in that case)."""
    q = int(qty)
    if q < 3:
        return []
    base = q // 3
    return [base, base, q - 2 * base]


# ── Earnings dates cache ──────────────────────────────────────────────────────
# Which strategies apply the earnings avoidance filter
SKIP_EARNINGS_STRATEGIES = {"trend_pullback"}
_EARNINGS_CACHE: dict[str, list[pd.Timestamp]] = {}

def _get_earnings_dates(ticker: str) -> list[pd.Timestamp]:
    """Get historical earnings dates for a ticker, cached globally.

    Returns sorted list of Timestamps (timezone-naive).
    """
    if ticker not in _EARNINGS_CACHE:
        import yfinance as yf
        try:
            t = yf.Ticker(ticker)
            ed = t.earnings_dates
            if ed is not None and not ed.empty:
                dates = sorted(pd.DatetimeIndex(ed.index).tz_localize(None).tolist())
                _EARNINGS_CACHE[ticker] = dates
            else:
                _EARNINGS_CACHE[ticker] = []
        except Exception:
            _EARNINGS_CACHE[ticker] = []
    return _EARNINGS_CACHE[ticker]


def add_earnings_filter(df: pd.DataFrame, ticker: str, p: StrategyParams = PARAMS) -> pd.DataFrame:
    """Add 'near_earnings' column: True if date is within N trading days before earnings."""
    out = df.copy()
    out["near_earnings"] = False
    earnings_dates = _get_earnings_dates(ticker)
    if not earnings_dates:
        return out
    avoid_days = p.earnings_avoid_days
    df_end = out.index[-1]
    for ed in earnings_dates:
        # Only consider earnings dates that fall within (or slightly before) the data window
        if ed > df_end:
            continue
        # Find the last trading day on or before the earnings date
        mask = out.index <= ed
        if not mask.any():
            continue
        last_before = out.index[mask][-1]
        pos = out.index.get_loc(last_before)
        # Mark the N rows before this position
        start = max(0, pos - avoid_days)
        out.iloc[start:pos, out.columns.get_loc("near_earnings")] = True
    return out


# ── Indicators ───────────────────────────────────────────────────────────────

def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """Return (macd_line, signal_line, histogram)."""
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger_bands(series: pd.Series, period: int = 20, n_std: float = 2.0):
    middle = sma(series, period)
    std = series.rolling(period, min_periods=period).std(ddof=0)
    upper = middle + n_std * std
    lower = middle - n_std * std
    return middle, upper, lower


def add_indicators(df: pd.DataFrame, p: StrategyParams = PARAMS) -> pd.DataFrame:
    """Attach all technical indicators to a daily OHLCV frame."""
    out = df.copy()
    # V1 indicators
    out["sma_fast"] = sma(out["close"], p.sma_fast)
    out["sma_slow"] = sma(out["close"], p.sma_slow)
    out["rsi"] = rsi(out["close"], p.rsi_period)
    out["atr"] = atr(out["high"], out["low"], out["close"], p.atr_period)
    out["sma_vol"] = sma(out["volume"], 20)
    bb_mid, bb_upper, bb_lower = bollinger_bands(out["close"], 20, p.mr_bollinger_mult)
    out["bb_mid"] = bb_mid
    out["bb_upper"] = bb_upper
    out["bb_lower"] = bb_lower

    # V2: MACD
    macd_line, sig_line, hist = macd(out["close"], p.macd_fast, p.macd_slow, p.macd_signal)
    out["macd"] = macd_line
    out["macd_signal"] = sig_line
    out["macd_hist"] = hist

    # V2: Regime helpers
    out["ema_short"] = ema(out["close"], p.regime_sma_short)
    out["ema_long"] = ema(out["close"], p.regime_sma_long)
    out["atr_pct"] = out["atr"] / out["close"]

    return out


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class EntrySignal:
    date: pd.Timestamp
    entry_price: float
    stop_loss: float
    take_profit: float
    atr: float
    rsi: float
    strategy: str = "trend_pullback"
    tp1: float = 0.0
    tp2: float = 0.0
    tp3: float = 0.0

    def __post_init__(self):
        # Derive the 3-level ladder from the entry and full target unless the
        # caller already supplied tp3. All 6 strategy checkers get this for free.
        if not self.tp3:
            self.tp1, self.tp2, self.tp3 = split_take_profit(self.entry_price, self.take_profit)


@dataclass
class Trade:
    ticker: str
    entry_date: pd.Timestamp
    entry_price: float
    stop_loss: float
    take_profit: float
    exit_date: pd.Timestamp
    exit_price: float
    exit_reason: str
    bars_held: int
    shares: float
    pnl_dollars: float
    pnl_pct: float
    strategy: str = "trend_pullback"


# ═════════════════════════════════════════════════════════════════════════════
# Strategy A: Trend Pullback (V1)
# ═════════════════════════════════════════════════════════════════════════════

def check_entry_trend_pullback(df: pd.DataFrame, idx: int, p: StrategyParams = PARAMS) -> Optional[EntrySignal]:
    if idx < max(p.sma_slow, p.rsi_period, p.atr_period) + 1:
        return None
    row = df.iloc[idx]
    prev = df.iloc[idx - 1]
    if pd.isna(row["sma_slow"]) or pd.isna(row["atr"]):
        return None
    # Skip entries near earnings to avoid gap risk
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
        strategy="trend_pullback",
    )


# ═════════════════════════════════════════════════════════════════════════════
# Strategy B: Breakout (V1)
# ═════════════════════════════════════════════════════════════════════════════

def check_entry_breakout(df: pd.DataFrame, idx: int, p: StrategyParams = PARAMS) -> Optional[EntrySignal]:
    if idx < max(p.sma_slow, p.rsi_period, p.atr_period, p.breakout_lookback) + 1:
        return None
    row = df.iloc[idx]
    if pd.isna(row["sma_slow"]) or pd.isna(row["atr"]) or pd.isna(row["sma_vol"]):
        return None
    if not (row["close"] > row["sma_slow"]):
        return None
    look = df.iloc[idx - p.breakout_range_lookback: idx + 1]
    recent_range = (look["high"] - look["low"]).mean()
    avg_range = look["high"].rolling(p.breakout_range_lookback).mean() - look["low"].rolling(p.breakout_range_lookback).mean()
    avg_range_val = float(avg_range.iloc[-1]) if pd.notna(avg_range.iloc[-1]) else recent_range
    if recent_range > avg_range_val * p.breakout_range_mult and not np.isnan(avg_range_val):
        return None
    if pd.isna(row["sma_vol"]) or row["volume"] < row["sma_vol"] * p.breakout_volume_mult:
        return None
    high_window = df["high"].iloc[max(0, idx - p.breakout_lookback): idx]
    prior_high = high_window.max()
    if pd.isna(prior_high) or not (float(row["high"]) > float(prior_high)):
        return None
    prev = df.iloc[idx - 1]
    if row["rsi"] < 50 or row["rsi"] <= prev["rsi"]:
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
        strategy="breakout",
    )


# ═════════════════════════════════════════════════════════════════════════════
# Strategy C: Mean Reversion (V1)
# ═════════════════════════════════════════════════════════════════════════════

def check_entry_mean_reversion(df: pd.DataFrame, idx: int, p: StrategyParams = PARAMS) -> Optional[EntrySignal]:
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
    above_bb = pd.isna(row["bb_lower"]) or row["close"] <= row["bb_lower"]
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
        strategy="mean_reversion",
    )


# ═════════════════════════════════════════════════════════════════════════════
# Strategy D: MACD Momentum (V2)
# ═════════════════════════════════════════════════════════════════════════════

def check_entry_momentum_macd(df: pd.DataFrame, idx: int, p: StrategyParams = PARAMS) -> Optional[EntrySignal]:
    """Buy when MACD crosses above signal line with rising RSI momentum."""
    if idx < max(p.sma_slow, p.rsi_period, p.atr_period, p.macd_slow) + 3:
        return None
    row = df.iloc[idx]
    prev = df.iloc[idx - 1]

    if pd.isna(row["sma_slow"]) or pd.isna(row["macd"]) or pd.isna(row["atr"]):
        return None

    # 1. Uptrend context
    if not (row["close"] > row["sma_slow"]):
        return None

    # 2. MACD histogram just turned positive (cross above signal)
    if not (row["macd_hist"] > 0 and prev["macd_hist"] <= 0):
        return None

    # 3. RSI > 50 and rising (momentum confirmation)
    if not (row["rsi"] > 50 and row["rsi"] > prev["rsi"]):
        return None

    # 4. Price > SMA(20) — short-term trend intact
    if pd.isna(row["sma_fast"]) or not (row["close"] > row["sma_fast"]):
        return None

    # 5. Volume confirmation (not strictly required but helps)
    if not pd.isna(row["sma_vol"]) and row["volume"] < row["sma_vol"] * 0.7:
        return None  # very low volume — skip

    entry = float(row["close"])
    sl = entry * (1.0 - p.macd_stop_loss_pct)
    tp_raw = entry + p.macd_tp_multiple * float(row["atr"])
    tp_pct = float(np.clip((tp_raw - entry) / entry, p.macd_tp_floor_pct, p.macd_tp_cap_pct))
    tp = entry * (1.0 + tp_pct)

    return EntrySignal(
        date=row.name if isinstance(row.name, pd.Timestamp) else pd.Timestamp(row.name),
        entry_price=entry, stop_loss=sl, take_profit=tp,
        atr=float(row["atr"]), rsi=float(row["rsi"]),
        strategy="momentum_macd",
    )


# ═════════════════════════════════════════════════════════════════════════════
# Strategy E: Regime Adaptive (V2)
# ═════════════════════════════════════════════════════════════════════════════

def _detect_regime(df: pd.DataFrame, idx: int, p: StrategyParams) -> str:
    """Classify market regime as 'risk_on', 'risk_off', or 'neutral'."""
    row = df.iloc[idx]
    if pd.isna(row["ema_short"]) or pd.isna(row["ema_long"]):
        return "neutral"

    # Trend-following regime: short EMA above long EMA = risk on
    if row["ema_short"] > row["ema_long"] and row["close"] > row["sma_slow"]:
        return "risk_on"

    # Short EMA below long EMA = risk off
    if row["ema_short"] < row["ema_long"] and row["close"] < row["sma_slow"]:
        return "risk_off"

    return "neutral"


def check_entry_regime(df: pd.DataFrame, idx: int, p: StrategyParams = PARAMS) -> Optional[EntrySignal]:
    """Regime-adaptive strategy: risk_on = aggressive, risk_off = defensive."""
    if idx < max(p.sma_slow, p.rsi_period, p.atr_period, p.regime_lookback) + 2:
        return None
    row = df.iloc[idx]
    prev = df.iloc[idx - 1]

    if pd.isna(row["sma_slow"]) or pd.isna(row["atr"]) or pd.isna(row["ema_long"]):
        return None

    regime = _detect_regime(df, idx, p)

    if regime == "risk_on":
        # Aggressive: buy dips in strong uptrend
        if not (row["close"] > row["ema_short"] > row["ema_long"]):
            return None
        if not (row["rsi"] > 45 and row["close"] > prev["close"]):
            return None
        sl_mult = p.regime_risk_on_mult * p.stop_loss_pct
    elif regime == "risk_off":
        # Defensive: only buy oversold bounces
        if not (row["close"] < row["ema_short"] and row["rsi"] < 40):
            return None
        if not (row["close"] > prev["close"]):  # bounce starting
            return None
        sl_mult = p.stop_loss_pct  # standard SL
    else:
        # Neutral: standard trend pullback-ish
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


# ═════════════════════════════════════════════════════════════════════════════
# Strategy F: Ensemble (V2) — weighted vote of all strategies
# ═════════════════════════════════════════════════════════════════════════════

def _get_all_signals(df: pd.DataFrame, idx: int, p: StrategyParams) -> dict[str, Optional[EntrySignal]]:
    """Collect signals from all base strategies at this bar."""
    return {
        "trend_pullback": check_entry_trend_pullback(df, idx, p),
        "breakout": check_entry_breakout(df, idx, p),
        "mean_reversion": check_entry_mean_reversion(df, idx, p),
        "momentum_macd": check_entry_momentum_macd(df, idx, p),
        "regime": check_entry_regime(df, idx, p),
    }


# Strategy weights — tuned by autoresearch (2026-05-28: rebalanced based on actual P&L)
# Baseline: trend:0.30, breakout:0.25, mr:0.10, macd:0.20, regime:0.15 → Ensemble 2025: -$28.15
# New: regime weighted highest (best performer +$333), breakout/mean_reversion reduced
ENSEMBLE_WEIGHTS = {
    "trend_pullback": 0.20,
    "breakout": 0.15,
    "mean_reversion": 0.05,
    "momentum_macd": 0.25,
    "regime": 0.35,
}


def check_entry_ensemble(df: pd.DataFrame, idx: int, p: StrategyParams = PARAMS) -> Optional[EntrySignal]:
    """Ensemble: enter when weighted vote of all strategies exceeds threshold."""
    if idx < 60:
        return None

    row = df.iloc[idx]
    signals = _get_all_signals(df, idx, p)
    voting = {name: sig is not None for name, sig in signals.items()}

    # Weighted vote
    score = sum(ENSEMBLE_WEIGHTS.get(name, 0) for name, voted in voting.items() if voted)

    # Require min_votes or weighted score >= threshold
    if score < 0.30:  # at least 30% weighted agreement (tightened from 0.25)
        return None

    # Use ensemble-specific risk params
    sl = float(row["close"]) * (1.0 - p.ensemble_stop_loss_pct)
    tp_raw = float(row["close"]) + p.ensemble_tp_multiple * float(row["atr"])
    tp_pct = (tp_raw - float(row["close"])) / float(row["close"])
    tp_pct = float(np.clip(tp_pct, p.ensemble_tp_floor_pct, p.ensemble_tp_cap_pct))
    tp = float(row["close"]) * (1.0 + tp_pct)

    return EntrySignal(
        date=row.name if isinstance(row.name, pd.Timestamp) else pd.Timestamp(row.name),
        entry_price=float(row["close"]),
        stop_loss=sl,
        take_profit=tp,
        atr=float(row["atr"]),
        rsi=float(row["rsi"]),
        strategy=f"ensemble_{score:.2f}",
    )


# ═════════════════════════════════════════════════════════════════════════════
# Strategy dispatch
# ═════════════════════════════════════════════════════════════════════════════

ENTRY_CHECKERS = {
    StrategyType.TREND_PULLBACK: check_entry_trend_pullback,
    StrategyType.BREAKOUT: check_entry_breakout,
    StrategyType.MEAN_REVERSION: check_entry_mean_reversion,
    StrategyType.MOMENTUM_MACD: check_entry_momentum_macd,
    StrategyType.REGIME_ADAPTIVE: check_entry_regime,
    StrategyType.ENSEMBLE: check_entry_ensemble,
}


def get_entry_checker(strategy: StrategyType):
    return ENTRY_CHECKERS[strategy]


def check_entry(df: pd.DataFrame, idx: int, p: StrategyParams = PARAMS) -> Optional[EntrySignal]:
    return ENTRY_CHECKERS[p.strategy](df, idx, p)


# ═════════════════════════════════════════════════════════════════════════════
# Trade simulation (shared across all strategies)
# ═════════════════════════════════════════════════════════════════════════════

def _max_holding_days(signal: EntrySignal, p: StrategyParams) -> int:
    """Return the max holding days for this signal's strategy."""
    s = signal.strategy
    if s.startswith("breakout"):
        return p.breakout_max_holding_days
    if s.startswith("mean_reversion"):
        return p.mr_max_holding_days
    if s.startswith("momentum_macd"):
        return p.macd_max_holding_days
    if s.startswith("ensemble"):
        return p.ensemble_max_holding_days
    return p.max_holding_days  # trend_pullback, regime


def simulate_exit(
    df: pd.DataFrame, entry_idx: int, signal: EntrySignal, p: StrategyParams = PARAMS
) -> tuple[pd.Timestamp, float, str, int]:
    """Walk forward from the bar AFTER entry, find which exit hits first.

    Priority: stop_loss → take_profit → time_stop (only if close >= entry) → end_of_data.

    The time stop fires after max_holding_days bars but is skipped if the position
    is underwater — in that case the trade keeps running until SL or TP is hit.
    """
    sl = signal.stop_loss
    tp = signal.take_profit
    entry_price = signal.entry_price
    max_days = _max_holding_days(signal, p)

    for i in range(entry_idx + 1, len(df)):
        bar = df.iloc[i]
        bars_held = i - entry_idx

        if bar["low"] <= sl:
            return bar.name, sl, "stop_loss", bars_held
        if bar["high"] >= tp:
            return bar.name, tp, "take_profit", bars_held
        # Time stop: only exit if at breakeven or better to avoid locking in a loss
        if bars_held >= max_days and float(bar["close"]) >= entry_price:
            return bar.name, float(bar["close"]), "time_stop", bars_held

    last = df.iloc[len(df) - 1]
    return last.name, float(last["close"]), "end_of_data", len(df) - 1 - entry_idx


@dataclass
class ExitLeg:
    exit_date: pd.Timestamp
    exit_price: float
    reason: str          # tp1 | tp2 | tp3 | stop_loss | time_stop | end_of_data
    bars_held: int
    fraction: float      # portion of the original position this leg closes


def simulate_exit_scaleout(
    df: pd.DataFrame, entry_idx: int, signal: EntrySignal, p: StrategyParams = PARAMS
) -> list[ExitLeg]:
    """Walk forward producing per-leg exits for the 3-TP / stepped-stop model.

    Per bar: check the stop FIRST (conservative) against the *current* floor, then
    TP1->TP2->TP3 in order. A TP fill raises the stepped floor effective the NEXT
    bar (breakeven after TP1, TP1 price after TP2). Time-stop / end-of-data close
    whatever remains.
    """
    from config import TP_SPLITS
    entry = signal.entry_price
    tps = [signal.tp1, signal.tp2, signal.tp3]
    fracs = list(TP_SPLITS)
    stop = signal.stop_loss
    max_days = _max_holding_days(signal, p)

    legs: list[ExitLeg] = []
    tp_hit = [False, False, False]
    remaining = 1.0

    for i in range(entry_idx + 1, len(df)):
        bar = df.iloc[i]
        bars_held = i - entry_idx

        # 1) stop first, against the floor as of the previous bar
        if float(bar["low"]) <= stop:
            legs.append(ExitLeg(bar.name, stop, "stop_loss", bars_held, remaining))
            return legs

        # 2) take-profits in ascending order (a wide bar can fill several)
        for k in range(3):
            if not tp_hit[k] and float(bar["high"]) >= tps[k]:
                tp_hit[k] = True
                legs.append(ExitLeg(bar.name, tps[k], f"tp{k+1}", bars_held, fracs[k]))
                remaining -= fracs[k]

        if tp_hit[2]:
            return legs  # fully scaled out

        # 3) raise the stepped floor (effective next bar)
        if tp_hit[1]:
            stop = max(stop, signal.tp1)
        elif tp_hit[0]:
            stop = max(stop, entry)

        # 4) time-stop on the remainder (only at breakeven+)
        if bars_held >= max_days and float(bar["close"]) >= entry and remaining > 1e-9:
            legs.append(ExitLeg(bar.name, float(bar["close"]), "time_stop", bars_held, remaining))
            return legs

    if remaining > 1e-9:
        last = df.iloc[len(df) - 1]
        legs.append(ExitLeg(last.name, float(last["close"]), "end_of_data",
                            len(df) - 1 - entry_idx, remaining))
    return legs


# ═════════════════════════════════════════════════════════════════════════════
# Backtest (per-ticker, per-strategy)
# ═════════════════════════════════════════════════════════════════════════════

def backtest_ticker(
    df: pd.DataFrame,
    ticker: str,
    window_start: pd.Timestamp,
    p: StrategyParams = PARAMS,
    strategy: Optional[StrategyType] = None,
) -> list[Trade]:
    strategy = strategy or p.strategy
    entry_checker = get_entry_checker(strategy)

    df = add_indicators(df, p)

    # Add earnings filter for strategies vulnerable to gap risk
    strat_name = strategy.value if isinstance(strategy, StrategyType) else strategy
    if strat_name in SKIP_EARNINGS_STRATEGIES:
        df = add_earnings_filter(df, ticker, p)

    trades: list[Trade] = []
    in_trade_until: int = -1

    for idx in range(len(df)):
        ts = df.index[idx]
        if ts < window_start:
            continue
        if p.one_position_per_ticker and idx <= in_trade_until:
            continue

        sig = entry_checker(df, idx, p)
        if sig is None:
            continue

        # Only enter if the TP target is reachable within 2 trading days
        if not is_tp_reachable_in_days(sig.entry_price, sig.take_profit, sig.atr, days=4):
            continue

        exit_date, exit_price, reason, bars = simulate_exit(df, idx, sig, p)
        shares = p.dollars_per_trade / sig.entry_price
        pnl_dollars = (exit_price - sig.entry_price) * shares
        pnl_pct = (exit_price - sig.entry_price) / sig.entry_price

        trades.append(Trade(
            ticker=ticker,
            entry_date=sig.date,
            entry_price=sig.entry_price,
            stop_loss=sig.stop_loss,
            take_profit=sig.take_profit,
            exit_date=exit_date if isinstance(exit_date, pd.Timestamp) else pd.Timestamp(exit_date),
            exit_price=exit_price,
            exit_reason=reason,
            bars_held=bars,
            shares=shares,
            pnl_dollars=pnl_dollars,
            pnl_pct=pnl_pct,
            strategy=strategy.value if isinstance(strategy, StrategyType) else strategy,
        ))
        in_trade_until = idx + bars

    return trades


run_ticker_backtest = backtest_ticker