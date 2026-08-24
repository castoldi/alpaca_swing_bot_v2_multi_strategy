"""Market-wide regime gate — blocks new entries while the broad market falls.

Implements the signal recommended by ``docs/bear-markets-and-crashes.md`` §8:
SPY drawdown off its trailing 252-day high. That document selected the signal on
1990-2026 daily index history covering four >=20% bears — data the bot's own
Alpaca-sourced backtests (2016+) never see — so testing it here is an
out-of-sample check of a pre-registered hypothesis, not a fit to 2022.

The gate blocks *new entries only*. Exits, open positions, and broker-held
TP/SL brackets are untouched, matching the daily-loss kill switch's existing
non-interference contract.

Point-in-time discipline: a decision for bar ``t`` uses only SPY daily bars that
closed strictly before ``t``'s calendar date, so an intraday 4h entry can never
see its own day's close. This mirrors ``data_feed.completed_bars``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from logger_setup import get_logger

log = get_logger(__name__)

GATE_TICKER = "SPY"
# Trading days in a year — the "52-week high" lookback from the source doc.
LOOKBACK_BARS = 252
SMA_BARS = 200


@dataclass(frozen=True)
class MarketRegimeGate:
    """Point-in-time answer to 'is the broad market in a confirmed decline?'.

    ``days`` and ``flags`` are parallel arrays sorted by date; ``flags[i]`` is
    the verdict computed from the bar that closed on ``days[i]``.
    """

    days: np.ndarray
    flags: np.ndarray
    mode: str

    def blocked(self, timestamp) -> bool:
        """True when new entries should be skipped as of ``timestamp``.

        Uses the latest SPY bar that closed strictly before this calendar date.
        Fails **open** (returns False) when no prior bar exists, so a missing
        warmup window can never silently halt all trading.
        """
        if self.mode == "off" or self.days.size == 0:
            return False
        as_of = np.datetime64(pd.Timestamp(timestamp).date(), "D")
        # searchsorted 'left' → first index >= as_of; the bar before it is the
        # last one that closed strictly earlier.
        idx = int(np.searchsorted(self.days, as_of, side="left")) - 1
        if idx < 0:
            return False
        return bool(self.flags[idx])

    @property
    def blocked_fraction(self) -> float:
        """Share of covered days the gate was active — sanity/whipsaw check."""
        if self.flags.size == 0:
            return 0.0
        return float(self.flags.mean())


def _load_daily(ticker: str, start: date, end: date) -> pd.DataFrame:
    """Fetch daily bars for ``ticker`` with a warmup year ahead of ``start``."""
    from market_cache import MarketDataCache

    cache = MarketDataCache()
    # 400 calendar days ≈ 252 trading days of warmup for the rolling high.
    warmup_start = start - timedelta(days=400)
    bars = cache.get_bars(ticker, warmup_start, end + timedelta(days=1), "1d", feed="sip")
    return bars


def build_gate(
    start: date,
    end: date,
    mode: str = "drawdown",
    threshold: float = 0.10,
    ticker: str = GATE_TICKER,
) -> MarketRegimeGate:
    """Build a gate over ``[start, end]``.

    ``mode``:
      - ``"drawdown"`` — block while SPY is ``threshold`` or more below its
        trailing 252-bar high (the doc's recommended signal).
      - ``"sma200"``   — block while SPY closes below its 200-bar SMA (the
        alternative markov-and-garch.md endorsed; tested here as the control).
      - ``"off"``      — never block (baseline).
    """
    if mode == "off":
        return MarketRegimeGate(np.array([], dtype="datetime64[D]"), np.array([], dtype=bool), "off")

    bars = _load_daily(ticker, start, end)
    if bars.empty:
        log.warning("Market regime gate: no %s data — gate disabled (fails open)", ticker)
        return MarketRegimeGate(np.array([], dtype="datetime64[D]"), np.array([], dtype=bool), "off")

    close = bars["close"].astype(float)
    if mode == "sma50":
        sma = close.rolling(50, min_periods=50).mean()
        flags = (close < sma).fillna(False)
    elif mode == "drawdown":
        # min_periods=1 → an expanding high until a full year is available.
        # Alpaca history begins ~2016, so 2016 itself runs on a partial window;
        # that is still strictly point-in-time, just a shorter lookback.
        rolling_high = close.rolling(LOOKBACK_BARS, min_periods=1).max()
        flags = (close / rolling_high - 1.0) <= -abs(threshold)
    elif mode == "sma200":
        sma = close.rolling(SMA_BARS, min_periods=SMA_BARS).mean()
        flags = close < sma
        flags = flags.fillna(False)
    else:
        raise ValueError(f"Unknown market regime mode '{mode}'")

    days = np.array(
        [np.datetime64(pd.Timestamp(ts).date(), "D") for ts in bars.index],
        dtype="datetime64[D]",
    )
    order = np.argsort(days)
    return MarketRegimeGate(days[order], flags.to_numpy(dtype=bool)[order], mode)
