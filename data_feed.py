"""Market data feed — 4h and daily OHLCV bars from Alpaca.

yfinance has no native 4h interval and only serves intraday history for the last
~730 days (so 2024 is unavailable). Alpaca's historical data API serves 4h bars
for all years, so the whole system (backtests, live bot, dashboard charts) sources
its candles here.

Everything downstream expects the V1/yfinance shape: a DataFrame with lowercase
``open/high/low/close/volume`` columns and a tz-naive (UTC) DatetimeIndex, sorted
ascending. This module normalizes Alpaca's output to exactly that.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Union
from zoneinfo import ZoneInfo

import pandas as pd

from config import ALPACA_KEY, ALPACA_SECRET, BAR_TIMEFRAME
from logger_setup import get_logger

log = get_logger(__name__)

_client = None
_OHLCV = ["open", "high", "low", "close", "volume"]
_ET = ZoneInfo("America/New_York")


def _get_client():
    global _client
    if _client is None:
        from alpaca.data.historical import StockHistoricalDataClient
        _client = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)
    return _client


def _as_dt(d: Union[date, datetime]) -> datetime:
    if isinstance(d, datetime):
        return d
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _normalize(raw: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Alpaca .df → the OHLCV/tz-naive shape the strategies expect."""
    if raw is None or len(raw) == 0:
        return pd.DataFrame(columns=_OHLCV)

    df = raw
    if isinstance(df.index, pd.MultiIndex):  # (symbol, timestamp)
        lvl0 = df.index.get_level_values(0)
        df = df.xs(ticker, level=0) if ticker in lvl0 else df.droplevel(0)

    df = df.rename(columns=str.lower)
    df = df[[c for c in _OHLCV if c in df.columns]].copy()

    idx = pd.to_datetime(df.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    df.index = idx

    if "volume" in df.columns:
        df = df[df["volume"] > 0]  # drop dead overnight 4h buckets (no trades)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def _timeframe_parts(timeframe: str) -> tuple[int, str]:
    if timeframe == "4h":
        return 4, "Hour"
    if timeframe == "1d":
        return 1, "Day"
    raise ValueError(f"Unsupported timeframe: {timeframe}")


def _feed_options(feed: str = "iex") -> dict:
    try:
        from alpaca.data.enums import Adjustment, DataFeed
    except Exception:
        return {}
    feeds = {"iex": DataFeed.IEX, "sip": DataFeed.SIP}
    normalized = feed.lower()
    if normalized not in feeds:
        raise ValueError(f"Unsupported stock feed: {feed}")
    return {"feed": feeds[normalized], "adjustment": Adjustment.ALL}


def fetch_bars(
    ticker: str,
    start: Union[date, datetime],
    end: Union[date, datetime],
    timeframe: str = BAR_TIMEFRAME,
    *,
    feed: str = "iex",
    strict: bool = False,
) -> pd.DataFrame:
    """Bars for ``ticker`` in [start, end]. Empty DataFrame on failure."""
    try:
        amount, unit_name = _timeframe_parts(timeframe)
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame(amount, getattr(TimeFrameUnit, unit_name)),
            start=_as_dt(start),
            end=_as_dt(end),
            **_feed_options(feed),
        )
        bars = _get_client().get_stock_bars(req)
        return _normalize(bars.df, ticker)
    except Exception as e:
        if strict:
            raise
        log.warning("fetch_bars(%s, %s, %s) failed: %s", ticker, timeframe, feed, e)
        return pd.DataFrame(columns=_OHLCV)


def fetch_recent(
    ticker: str, days: int = 120, timeframe: str = BAR_TIMEFRAME
) -> pd.DataFrame:
    """Most recent bars for live trading and dashboard charts."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return fetch_bars(ticker, start, end, timeframe)


def completed_bars(
    df: pd.DataFrame, timeframe: str, as_of: datetime | None = None
) -> pd.DataFrame:
    """Remove any candle that could still be forming at ``as_of``.

    Alpaca includes the accumulating current bucket in bar responses, and the
    backtests only ever see completed candles — so live evaluations must drop
    the partial daily session (1d) or the partial 4-hour bucket (4h) before a
    strategy looks at the frame.
    """
    if df.empty:
        return df
    current = as_of or datetime.now(_ET)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_ET)
    if timeframe == "1d":
        session_date = current.astimezone(_ET).date()
        return df[pd.Index(df.index.date) < session_date].copy()
    if timeframe == "4h":
        # Index is tz-naive UTC bar-start; a bucket [T, T+4h) is complete
        # once now >= T+4h.
        now_utc = current.astimezone(timezone.utc).replace(tzinfo=None)
        cutoff = now_utc - timedelta(hours=4)
        return df[df.index <= cutoff].copy()
    return df


def fetch_4h(
    ticker: str, start: Union[date, datetime], end: Union[date, datetime]
) -> pd.DataFrame:
    """Compatibility wrapper for 4h bars."""
    return fetch_bars(ticker, start, end, "4h")


def fetch_recent_4h(ticker: str, days: int = 120) -> pd.DataFrame:
    """Compatibility wrapper for recent 4h bars."""
    return fetch_recent(ticker, days, "4h")


def fetch_snapshots(tickers: list[str]) -> dict:
    """Live per-ticker market snapshot for the dashboard.

    Uses Alpaca's snapshot endpoint (one request for the whole universe): latest
    trade price, today's daily bar, and the previous daily bar (for day change %).

    Returns ``{symbol: {price, prev_close, change, change_pct, day_high, day_low,
    day_open, volume, ts}}``. Missing/failed symbols are simply absent.
    """
    if not tickers:
        return {}
    try:
        from alpaca.data.requests import StockSnapshotRequest
        try:
            from alpaca.data.enums import DataFeed
            extra = {"feed": DataFeed.IEX}
        except Exception:
            extra = {}
        req = StockSnapshotRequest(symbol_or_symbols=list(tickers), **extra)
        snaps = _get_client().get_stock_snapshot(req)
    except Exception as e:
        log.warning("fetch_snapshots failed: %s", e)
        return {}

    out: dict[str, dict] = {}
    for sym, snap in (snaps or {}).items():
        if snap is None:
            continue
        try:
            daily = getattr(snap, "daily_bar", None)
            prev = getattr(snap, "previous_daily_bar", None)
            trade = getattr(snap, "latest_trade", None)

            price = None
            if trade is not None and getattr(trade, "price", None):
                price = float(trade.price)
            elif daily is not None and getattr(daily, "close", None):
                price = float(daily.close)
            if price is None:
                continue

            prev_close = float(prev.close) if prev is not None and getattr(prev, "close", None) else None
            change = change_pct = None
            if prev_close:
                change = price - prev_close
                change_pct = change / prev_close * 100.0

            ts = getattr(trade, "timestamp", None) or getattr(daily, "timestamp", None)
            out[sym] = {
                "price": round(price, 2),
                "prev_close": round(prev_close, 2) if prev_close else None,
                "change": round(change, 2) if change is not None else None,
                "change_pct": round(change_pct, 2) if change_pct is not None else None,
                "day_high": round(float(daily.high), 2) if daily and getattr(daily, "high", None) else None,
                "day_low": round(float(daily.low), 2) if daily and getattr(daily, "low", None) else None,
                "day_open": round(float(daily.open), 2) if daily and getattr(daily, "open", None) else None,
                "volume": int(daily.volume) if daily and getattr(daily, "volume", None) else None,
                "ts": ts.isoformat() if ts is not None else None,
            }
        except Exception as e:
            log.debug("snapshot parse failed for %s: %s", sym, e)
    return out


# Convenience so callers can label what they ran on without importing config.
TIMEFRAME = BAR_TIMEFRAME
