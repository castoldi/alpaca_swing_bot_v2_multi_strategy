"""Base class, shared dataclasses, indicators, and exit engine for all strategies."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Optional

import numpy as np
import pandas as pd

from config import BAR_TIMEFRAME, PARAMS, StrategyParams, TP_SPLITS
from holding_period import holding_deadline, holding_sessions_limit, utc


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

SKIP_EARNINGS_STRATEGIES: set[str] = {"trend_pullback", "ensemble"}


def add_earnings_filter(df: pd.DataFrame, ticker: str, p: StrategyParams = PARAMS,
                        **policy_options) -> pd.DataFrame:
    from earnings_calendar import apply_filter
    return apply_filter(df, ticker, p, **policy_options)


# ── Indicators ────────────────────────────────────────────────────────────────

def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI using the existing first-change-seeded exponential smoothing.

    Require ``period`` observed price changes (period + 1 closes on complete
    data). Warmup stays NaN; mature gains-only/losses-only/flat histories are
    100/0/50 respectively. Flat 50 is a convention, not a warmup substitute.
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    out.loc[(avg_gain > 0) & (avg_loss == 0)] = 100.0
    out.loc[(avg_gain == 0) & (avg_loss > 0)] = 0.0
    out.loc[(avg_gain == 0) & (avg_loss == 0)] = 50.0
    return out


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


def tsi(close: pd.Series, long: int = 25, short: int = 13, signal: int = 13):
    """True Strength Index — double-EMA-smoothed momentum, plus its signal line.

    Steadier than MACD (two smoothing passes instead of one), so it produces far
    fewer false crossovers on a leveraged ETF's noise. Returns (tsi, signal).
    """
    momentum = close.diff()
    smoothed = ema(ema(momentum, long), short)
    smoothed_abs = ema(ema(momentum.abs(), long), short)
    line = 100.0 * smoothed / smoothed_abs.replace(0.0, np.nan)
    return line, ema(line, signal)


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
    out["sma_cross"] = sma(out["close"], p.sma_cross_period)
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
    tsi_line, tsi_sig = tsi(
        out["close"], p.tqqq_tsi_long, p.tqqq_tsi_short, p.tqqq_tsi_signal
    )
    out["tsi"] = tsi_line
    out["tsi_signal"] = tsi_sig
    out["ema_trend"] = ema(out["close"], p.tqqq_ema_period)
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
        if self.take_profit > 0 and not self.tp3:
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
    timeframe: ClassVar[str] = BAR_TIMEFRAME
    exit_mode: ClassVar[str] = "bracket"
    has_take_profit: ClassVar[bool] = True
    # None = trade the shared config.TICKERS universe. A strategy tuned for a
    # specific instrument (e.g. a leveraged ETF) overrides this, which both
    # scopes it to those symbols and keeps every other strategy off them.
    tickers: ClassVar[Optional[tuple[str, ...]]] = None
    # Exit reason recorded when a `signal_with_stop` strategy's check_exit fires.
    signal_exit_reason: ClassVar[str] = "sma_cross_down"

    def __init__(self):
        self.enabled: bool = True

    def universe(self) -> list[str]:
        """Symbols this strategy trades."""
        return strategy_universe(self)

    def stop_loss_fraction(self, p: StrategyParams = PARAMS) -> float:
        """Emergency-stop distance for `signal_with_stop` strategies."""
        return p.sma_cross_stop_loss_pct

    @abstractmethod
    def check_entry(self, df: pd.DataFrame, idx: int, p: StrategyParams = PARAMS) -> Optional[EntrySignal]:
        ...

    def check_exit(
        self, df: pd.DataFrame, idx: int, p: StrategyParams = PARAMS
    ) -> Optional[str]:
        return None

    def meta(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "version": self.version,
            "color": self.color,
            "description": self.description,
            "params_display": list(self.params_display),
            "timeframe": self.timeframe,
            "exit_mode": self.exit_mode,
            "has_take_profit": self.has_take_profit,
            "tickers": list(self.universe()),
            "enabled": self.enabled,
        }


# ── Universe resolution ───────────────────────────────────────────────────────

def strategy_universe(strategy, default: list[str] | None = None) -> list[str]:
    """Symbols ``strategy`` trades.

    A strategy that declares ``tickers`` is scoped to exactly those (that is what
    keeps ``tqqq_momentum`` on TQQQ and every other strategy off it). Otherwise it
    trades ``default``, or the shared ``config.TICKERS`` when no default is given.
    Callers pass their own module-level TICKERS so it stays monkeypatchable, and
    ``getattr`` keeps this working for duck-typed strategies in tests.
    """
    tickers = getattr(strategy, "tickers", None)
    if tickers:
        return list(tickers)
    if default is not None:
        return list(default)
    from config import TICKERS
    return list(TICKERS)


# ── Shared exit engine ────────────────────────────────────────────────────────

def _max_holding_days(signal: EntrySignal, p: StrategyParams) -> int:
    """Compatibility name; configuration values count exchange sessions."""
    return holding_sessions_limit(signal.strategy, p)


def _holding_clock(df, entry_idx, signal, p, entry_at_open):
    """Legacy candle simulation uses the same deadline, at observed closes."""
    from backtest_execution import signal_availability
    closes = signal_availability(df.index, df.attrs.get('timeframe', BAR_TIMEFRAME))
    fill_at = utc(df.index[entry_idx]) if entry_at_open else closes[entry_idx]
    return holding_deadline(fill_at, _max_holding_days(signal, p)), closes


def simulate_exit(
    df: pd.DataFrame, entry_idx: int, signal: EntrySignal, p: StrategyParams = PARAMS,
    *, entry_at_open: bool = False,
) -> tuple[pd.Timestamp, float, str, int]:
    """Simulate barriers after a close entry, or on an explicitly modeled open.

    For an open fill, include that bar's range and count elapsed bars from the
    fill (zero on its own bar), matching the signal-exit engine's convention.
    The signal supplied by the caller carries the executed entry basis.
    """
    sl = signal.stop_loss
    tp = signal.take_profit
    entry_price = signal.entry_price
    deadline, closes = _holding_clock(df, entry_idx, signal, p, entry_at_open)

    for i in range(entry_idx if entry_at_open else entry_idx + 1, len(df)):
        bar = df.iloc[i]
        bars_held = i - entry_idx
        if float(bar['open']) < sl:
            return bar.name, float(bar['open']), 'gap_stop', bars_held
        if bar["low"] <= sl:
            return bar.name, sl, "stop_loss", bars_held
        if bar["high"] >= tp:
            return bar.name, tp, "take_profit", bars_held
        if closes[i] >= deadline and float(bar["close"]) >= entry_price:
            return closes[i], float(bar["close"]), "time_stop", bars_held

    last = df.iloc[len(df) - 1]
    return last.name, float(last["close"]), "end_of_data", len(df) - 1 - entry_idx


def simulate_exit_scaleout(
    df: pd.DataFrame, entry_idx: int, signal: EntrySignal, p: StrategyParams = PARAMS,
    *, entry_at_open: bool = False,
) -> list[ExitLeg]:
    """Research scale-out with the same entry clock as ``simulate_exit``."""
    entry = signal.entry_price
    tps = [signal.tp1, signal.tp2, signal.tp3]
    fracs = list(TP_SPLITS)
    stop = signal.stop_loss
    deadline, closes = _holding_clock(df, entry_idx, signal, p, entry_at_open)

    legs: list[ExitLeg] = []
    tp_hit = [False, False, False]
    remaining = 1.0

    for i in range(entry_idx if entry_at_open else entry_idx + 1, len(df)):
        bar = df.iloc[i]
        bars_held = i - entry_idx

        if float(bar['open']) < stop:
            legs.append(ExitLeg(bar.name, float(bar['open']), 'gap_stop', bars_held, remaining))
            return legs
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

        if closes[i] >= deadline and float(bar["close"]) >= entry and remaining > 1e-9:
            legs.append(ExitLeg(closes[i], float(bar["close"]), "time_stop", bars_held, remaining))
            return legs

    if remaining > 1e-9:
        last = df.iloc[len(df) - 1]
        legs.append(ExitLeg(last.name, float(last["close"]), "end_of_data",
                            len(df) - 1 - entry_idx, remaining))
    return legs


# ── Backtest (per-ticker) ─────────────────────────────────────────────────────

def backtest_signal_exit_ticker(
    df: pd.DataFrame,
    ticker: str,
    window_start: pd.Timestamp,
    p: StrategyParams,
    strategy: BaseStrategy,
) -> list[Trade]:
    """Compatibility wrapper for close-confirmed signal strategies."""
    return backtest_ticker(df, ticker, window_start, p, strategy)

def backtest_ticker(
    df: pd.DataFrame,
    ticker: str,
    window_start: pd.Timestamp,
    p: StrategyParams = PARAMS,
    strategy: Optional[BaseStrategy] = None,
    *, execution_bars: pd.DataFrame | None = None,
    legacy_execution: bool = False,
) -> list[Trade]:
    if strategy is None:
        raise ValueError("strategy must be a BaseStrategy instance")
    if df.empty:
        return []

    from backtest_portfolio import (
        collect_backtest_candidates,
        run_annual_portfolio,
    )

    window_end = pd.Timestamp(df.index[-1])
    if not legacy_execution:
        # df.index[-1] is the START of the last signal candle. The minute-level
        # window filter in collect_session_candidates requires a fill's whole
        # minute to lie at or before window_end, so a boundary at the candle's
        # own start timestamp excludes every minute within that candle's
        # duration — including the one a signal from it would fill on.
        from backtest_execution import signal_availability
        window_end = pd.Timestamp(signal_availability(df.index[-1:], strategy.timeframe)[-1])

    candidates = collect_backtest_candidates(
        df,
        ticker,
        pd.Timestamp(window_start),
        window_end,
        p,
        strategy,
        execution_bars=execution_bars,
        legacy_execution=legacy_execution,
    )
    result = run_annual_portfolio(
        candidates,
        initial_equity=p.initial_backtest_equity,
        position_fraction=p.position_size_pct,
        max_positions=p.max_concurrent_positions,
    )
    return list(result.trades)
