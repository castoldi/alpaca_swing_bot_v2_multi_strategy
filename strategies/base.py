"""Base class, shared dataclasses, indicators, and exit engine for all strategies."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Optional

import numpy as np
import pandas as pd

from config import PARAMS, StrategyParams, TP_SPLITS


# ── TP reachability filter ─────────────────────────────────────────────────────

def is_tp_reachable_in_days(entry_price: float, take_profit: float, atr: float, days: int = 2) -> bool:
    if atr <= 0 or entry_price <= 0:
        return False
    distance = take_profit - entry_price
    if distance <= 0:
        return True
    return distance <= days * atr


def split_take_profit(entry_price: float, take_profit: float) -> tuple[float, float, float]:
    d = take_profit - entry_price
    return (entry_price + d / 3.0, entry_price + 2.0 * d / 3.0, take_profit)


def split_qty(qty: int) -> list[int]:
    q = int(qty)
    if q < 3:
        return []
    base = q // 3
    return [base, base, q - 2 * base]


# ── Earnings filter ───────────────────────────────────────────────────────────

SKIP_EARNINGS_STRATEGIES: set[str] = {"trend_pullback"}
_EARNINGS_CACHE: dict[str, list[pd.Timestamp]] = {}


def _get_earnings_dates(ticker: str) -> list[pd.Timestamp]:
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
    out = df.copy()
    out["near_earnings"] = False
    earnings_dates = _get_earnings_dates(ticker)
    if not earnings_dates:
        return out
    avoid_days = p.earnings_avoid_days
    df_end = out.index[-1]
    for ed in earnings_dates:
        if ed > df_end:
            continue
        mask = out.index <= ed
        if not mask.any():
            continue
        last_before = out.index[mask][-1]
        pos = out.index.get_loc(last_before)
        start = max(0, pos - avoid_days)
        out.iloc[start:pos, out.columns.get_loc("near_earnings")] = True
    return out


# ── Indicators ────────────────────────────────────────────────────────────────

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
    out = df.copy()
    out["sma_fast"] = sma(out["close"], p.sma_fast)
    out["sma_slow"] = sma(out["close"], p.sma_slow)
    out["rsi"] = rsi(out["close"], p.rsi_period)
    out["atr"] = atr(out["high"], out["low"], out["close"], p.atr_period)
    out["sma_vol"] = sma(out["volume"], 20)
    bb_mid, bb_upper, bb_lower = bollinger_bands(out["close"], 20, p.mr_bollinger_mult)
    out["bb_mid"] = bb_mid
    out["bb_upper"] = bb_upper
    out["bb_lower"] = bb_lower
    macd_line, sig_line, hist = macd(out["close"], p.macd_fast, p.macd_slow, p.macd_signal)
    out["macd"] = macd_line
    out["macd_signal"] = sig_line
    out["macd_hist"] = hist
    out["ema_short"] = ema(out["close"], p.regime_sma_short)
    out["ema_long"] = ema(out["close"], p.regime_sma_long)
    out["atr_pct"] = out["atr"] / out["close"]
    return out


# ── Data classes ──────────────────────────────────────────────────────────────

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


@dataclass
class ExitLeg:
    exit_date: pd.Timestamp
    exit_price: float
    reason: str
    bars_held: int
    fraction: float


# ── Base Strategy interface ───────────────────────────────────────────────────

class BaseStrategy(ABC):
    name: ClassVar[str] = ""
    label: ClassVar[str] = ""
    version: ClassVar[str] = "V1"
    color: ClassVar[str] = "#60a5fa"
    description: ClassVar[str] = ""
    params_display: ClassVar[list[str]] = []

    def __init__(self):
        self.enabled: bool = True

    @abstractmethod
    def check_entry(self, df: pd.DataFrame, idx: int, p: StrategyParams = PARAMS) -> Optional[EntrySignal]:
        ...

    def meta(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "version": self.version,
            "color": self.color,
            "description": self.description,
            "params_display": list(self.params_display),
            "enabled": self.enabled,
        }


# ── Shared exit engine ────────────────────────────────────────────────────────

def _max_holding_days(signal: EntrySignal, p: StrategyParams) -> int:
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
        if bars_held >= max_days and float(bar["close"]) >= entry_price:
            return bar.name, float(bar["close"]), "time_stop", bars_held

    last = df.iloc[len(df) - 1]
    return last.name, float(last["close"]), "end_of_data", len(df) - 1 - entry_idx


def simulate_exit_scaleout(
    df: pd.DataFrame, entry_idx: int, signal: EntrySignal, p: StrategyParams = PARAMS
) -> list[ExitLeg]:
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

        if float(bar["low"]) <= stop:
            legs.append(ExitLeg(bar.name, stop, "stop_loss", bars_held, remaining))
            return legs

        for k in range(3):
            if not tp_hit[k] and float(bar["high"]) >= tps[k]:
                tp_hit[k] = True
                legs.append(ExitLeg(bar.name, tps[k], f"tp{k+1}", bars_held, fracs[k]))
                remaining -= fracs[k]

        if tp_hit[2]:
            return legs

        if tp_hit[1]:
            stop = max(stop, signal.tp1)
        elif tp_hit[0]:
            stop = max(stop, entry)

        if bars_held >= max_days and float(bar["close"]) >= entry and remaining > 1e-9:
            legs.append(ExitLeg(bar.name, float(bar["close"]), "time_stop", bars_held, remaining))
            return legs

    if remaining > 1e-9:
        last = df.iloc[len(df) - 1]
        legs.append(ExitLeg(last.name, float(last["close"]), "end_of_data",
                            len(df) - 1 - entry_idx, remaining))
    return legs


# ── Backtest (per-ticker) ─────────────────────────────────────────────────────

def backtest_ticker(
    df: pd.DataFrame,
    ticker: str,
    window_start: pd.Timestamp,
    p: StrategyParams = PARAMS,
    strategy: Optional[BaseStrategy] = None,
) -> list[Trade]:
    if strategy is None:
        raise ValueError("strategy must be a BaseStrategy instance")

    df = add_indicators(df, p)
    if strategy.name in SKIP_EARNINGS_STRATEGIES:
        df = add_earnings_filter(df, ticker, p)

    trades: list[Trade] = []
    in_trade_until: int = -1

    for idx in range(len(df)):
        ts = df.index[idx]
        if ts < window_start:
            continue
        if p.one_position_per_ticker and idx <= in_trade_until:
            continue

        sig = strategy.check_entry(df, idx, p)
        if sig is None:
            continue

        if not is_tp_reachable_in_days(sig.entry_price, sig.tp1, sig.atr, days=4):
            continue

        legs = simulate_exit_scaleout(df, idx, sig, p)
        if not legs:
            continue

        shares_total = p.dollars_per_trade / sig.entry_price
        for leg in legs:
            shares = shares_total * leg.fraction
            exit_date = leg.exit_date if isinstance(leg.exit_date, pd.Timestamp) else pd.Timestamp(leg.exit_date)
            trades.append(Trade(
                ticker=ticker,
                entry_date=sig.date,
                entry_price=sig.entry_price,
                stop_loss=sig.stop_loss,
                take_profit=sig.tp3,
                exit_date=exit_date,
                exit_price=leg.exit_price,
                exit_reason=leg.reason,
                bars_held=leg.bars_held,
                shares=shares,
                pnl_dollars=(leg.exit_price - sig.entry_price) * shares,
                pnl_pct=(leg.exit_price - sig.entry_price) / sig.entry_price,
                strategy=strategy.name,
            ))
        in_trade_until = idx + max(leg.bars_held for leg in legs)

    return trades
