"""Daily OHLCV bars from yfinance, for backtest history older than Alpaca's floor.

Alpaca's historical equity API (checked directly against this account, both IEX
and SIP feeds) returns no bars before 2016-01-04 for any symbol, regardless of
how much earlier the symbol actually listed. `data_feed.py` is the sole source
for everything the live bot and dashboard consume, and stays that way — this
module exists only to extend the **daily-timeframe** backtest report
(`backtest_history.py`, `sma_50_cross` specifically) back to 2008.

Not a general-purpose substitute for `data_feed`: yfinance has no 4h interval,
so a 4h strategy (ensemble, regime, breakout, momentum_macd, mean_reversion,
trend_pullback, tqqq_momentum) cannot be extended this way at any timeframe —
only `sma_50_cross`, which already trades daily bars, benefits.

``auto_adjust=True`` matches Alpaca's ``adjustment="all"``: both are fully
split+dividend adjusted, confirmed by comparing same-day closes across the
2016 boundary (ratio 1.0000 +/- 1e-4 on NVDA/AMZN/AMD) — the two series stitch
without a valuation jump at the handoff date.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Union

import pandas as pd
import yfinance as yf

from logger_setup import get_logger

log = get_logger(__name__)
_OHLCV = ["open", "high", "low", "close", "volume"]

# yfinance has no listing before this for any symbol (Yahoo's own equity
# history floor); a request older than this is clamped rather than failing.
EARLIEST_POSSIBLE = date(1962, 1, 2)


def _as_date(value: Union[date, datetime]) -> date:
    return value.date() if isinstance(value, datetime) else value


def fetch_bars(
    ticker: str,
    start: Union[date, datetime],
    end: Union[date, datetime],
    timeframe: str = "1d",
    *,
    feed: str | None = None,
    strict: bool = False,
) -> pd.DataFrame:
    """Daily bars for ``ticker`` in ``[start, end)``. Empty frame on failure.

    ``feed`` is accepted (and ignored) only so this matches the
    ``market_cache.Fetcher`` signature used for Alpaca fetches — yfinance has
    no feed concept. Only ``timeframe="1d"`` is supported; anything else
    raises, since silently returning daily bars for a 4h request would corrupt
    a strategy's indicator math without any visible sign of it.
    """
    if timeframe != "1d":
        raise ValueError(
            f"yfinance_history only serves daily bars, got timeframe={timeframe!r}"
        )
    try:
        raw = yf.download(
            ticker,
            start=_as_date(start),
            end=_as_date(end),
            interval="1d",
            auto_adjust=True,
            progress=False,
        )
        return _normalize(raw, ticker)
    except Exception as e:
        if strict:
            raise
        log.warning("yfinance_history.fetch_bars(%s) failed: %s", ticker, e)
        return _empty_frame()


def _empty_frame() -> pd.DataFrame:
    frame = pd.DataFrame(columns=_OHLCV)
    frame.index = pd.DatetimeIndex([], name="timestamp")
    return frame


def _normalize(raw: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if raw is None or raw.empty:
        return _empty_frame()

    frame = raw.copy()
    # A single-symbol yf.download() with recent yfinance versions returns a
    # 2-level column index (field, ticker) exactly like a multi-symbol call.
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    frame.columns = [str(c).lower() for c in frame.columns]

    missing = [c for c in _OHLCV if c not in frame.columns]
    if missing:
        raise ValueError(f"yfinance bars for {ticker} missing columns: {missing}")
    frame = frame[_OHLCV].astype(float)

    index = pd.to_datetime(frame.index)
    if getattr(index, "tz", None) is not None:
        index = index.tz_convert("UTC").tz_localize(None)
    frame.index = index
    frame.index.name = "timestamp"
    return frame[~frame.index.duplicated(keep="last")].sort_index()
