"""Persistent, incremental cache for historical Alpaca OHLCV bars."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

import data_feed


ROOT = Path(__file__).parent
CACHE_DB = ROOT / "cache" / "market_data.db"
OHLCV = ["open", "high", "low", "close", "volume"]

SeriesKey = tuple[str, str, str, str]
Fetcher = Callable[..., pd.DataFrame]


def _timestamp(value: date | datetime | pd.Timestamp) -> pd.Timestamp:
    """Normalize a boundary to a timezone-naive UTC timestamp."""
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC").tz_localize(None)
    return ts


def _completed_data_ceiling(now: datetime) -> pd.Timestamp:
    """Stable daily refresh boundary that excludes the current UTC session."""
    return _timestamp(now).normalize()


def _empty_frame() -> pd.DataFrame:
    frame = pd.DataFrame(columns=OHLCV)
    frame.index = pd.DatetimeIndex([], name="timestamp")
    return frame


class MarketDataCache:
    """SQLite read-through cache keyed by symbol, timeframe, feed, and adjustment."""

    def __init__(
        self,
        path: Path | str = CACHE_DB,
        fetcher: Fetcher | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path)
        self.fetcher = fetcher or data_feed.fetch_bars
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS bars (
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    feed TEXT NOT NULL,
                    adjustment TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL,
                    PRIMARY KEY (
                        symbol, timeframe, feed, adjustment, timestamp
                    )
                );
                CREATE TABLE IF NOT EXISTS coverage (
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    feed TEXT NOT NULL,
                    adjustment TEXT NOT NULL,
                    requested_start TEXT NOT NULL,
                    requested_end TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, timeframe, feed, adjustment)
                );
                CREATE INDEX IF NOT EXISTS idx_bars_series_time
                ON bars (symbol, timeframe, feed, adjustment, timestamp);
                """
            )

    def _coverage(self, key: SeriesKey) -> tuple[pd.Timestamp, pd.Timestamp] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT requested_start, requested_end
                FROM coverage
                WHERE symbol=? AND timeframe=? AND feed=? AND adjustment=?
                """,
                key,
            ).fetchone()
        if row is None:
            return None
        return pd.Timestamp(row["requested_start"]), pd.Timestamp(row["requested_end"])

    @staticmethod
    def _normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
        if frame is None:
            raise TypeError("Historical downloader returned None")
        if frame.empty:
            return _empty_frame()

        normalized = frame.rename(columns=str.lower).copy()
        missing = [column for column in OHLCV if column not in normalized.columns]
        if missing:
            raise ValueError(f"Historical bars missing columns: {', '.join(missing)}")
        normalized = normalized[OHLCV].astype(float)

        index = pd.to_datetime(normalized.index)
        if getattr(index, "tz", None) is not None:
            index = index.tz_convert("UTC").tz_localize(None)
        normalized.index = index
        normalized.index.name = "timestamp"
        return normalized[~normalized.index.duplicated(keep="last")].sort_index()

    def _store_segment(
        self,
        key: SeriesKey,
        requested_start: pd.Timestamp,
        requested_end: pd.Timestamp,
        frame: pd.DataFrame,
    ) -> None:
        normalized = self._normalize_frame(frame)
        records = [
            (
                *key,
                pd.Timestamp(timestamp).isoformat(),
                float(row.open),
                float(row.high),
                float(row.low),
                float(row.close),
                float(row.volume),
            )
            for timestamp, row in normalized.iterrows()
        ]

        with self._connect() as connection:
            if records:
                connection.executemany(
                    """
                    INSERT INTO bars (
                        symbol, timeframe, feed, adjustment, timestamp,
                        open, high, low, close, volume
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (
                        symbol, timeframe, feed, adjustment, timestamp
                    ) DO UPDATE SET
                        open=excluded.open,
                        high=excluded.high,
                        low=excluded.low,
                        close=excluded.close,
                        volume=excluded.volume
                    """,
                    records,
                )

            existing = connection.execute(
                """
                SELECT requested_start, requested_end
                FROM coverage
                WHERE symbol=? AND timeframe=? AND feed=? AND adjustment=?
                """,
                key,
            ).fetchone()
            if existing is None:
                combined_start = requested_start
                combined_end = requested_end
            else:
                combined_start = min(
                    pd.Timestamp(existing["requested_start"]), requested_start
                )
                combined_end = max(
                    pd.Timestamp(existing["requested_end"]), requested_end
                )

            connection.execute(
                """
                INSERT INTO coverage (
                    symbol, timeframe, feed, adjustment,
                    requested_start, requested_end, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (symbol, timeframe, feed, adjustment) DO UPDATE SET
                    requested_start=excluded.requested_start,
                    requested_end=excluded.requested_end,
                    updated_at=excluded.updated_at
                """,
                (
                    *key,
                    combined_start.isoformat(),
                    combined_end.isoformat(),
                    _timestamp(self.now_fn()).isoformat(),
                ),
            )

    def _read(
        self,
        key: SeriesKey,
        start: pd.Timestamp,
        end: pd.Timestamp,
    ) -> pd.DataFrame:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT timestamp, open, high, low, close, volume
                FROM bars
                WHERE symbol=? AND timeframe=? AND feed=? AND adjustment=?
                  AND timestamp>=? AND timestamp<?
                ORDER BY timestamp
                """,
                (*key, start.isoformat(), end.isoformat()),
            ).fetchall()
        if not rows:
            return _empty_frame()
        frame = pd.DataFrame([dict(row) for row in rows])
        frame.index = pd.to_datetime(frame.pop("timestamp"))
        frame.index.name = "timestamp"
        return frame[OHLCV]

    def get_bars(
        self,
        ticker: str,
        start: date | datetime,
        end: date | datetime,
        timeframe: str,
        *,
        feed: str = "sip",
        adjustment: str = "all",
    ) -> pd.DataFrame:
        """Return cached bars for ``[start, end)``, downloading missing edges."""
        normalized_feed = feed.lower()
        if normalized_feed not in {"iex", "sip"}:
            raise ValueError(f"Unsupported stock feed: {feed}")
        if adjustment.lower() != "all":
            raise ValueError("Only adjustment='all' is supported by this cache")

        start_ts = _timestamp(start)
        end_ts = min(_timestamp(end), _completed_data_ceiling(self.now_fn()))
        if end_ts <= start_ts:
            return _empty_frame()

        key: SeriesKey = (
            ticker.upper(),
            timeframe.lower(),
            normalized_feed,
            adjustment.lower(),
        )
        coverage = self._coverage(key)
        segments: list[tuple[pd.Timestamp, pd.Timestamp]] = []
        if coverage is None:
            segments.append((start_ts, end_ts))
        else:
            covered_start, covered_end = coverage
            if start_ts < covered_start:
                segments.append((start_ts, covered_start))
            if end_ts > covered_end:
                segments.append((covered_end, end_ts))

        for segment_start, segment_end in segments:
            frame = self.fetcher(
                ticker,
                segment_start.to_pydatetime(),
                segment_end.to_pydatetime(),
                timeframe,
                feed=normalized_feed,
                strict=True,
            )
            self._store_segment(key, segment_start, segment_end, frame)

        return self._read(key, start_ts, end_ts)

    def status(self) -> list[dict]:
        """Describe every cached series and its successful request coverage."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    c.symbol, c.timeframe, c.feed, c.adjustment,
                    c.requested_start, c.requested_end, c.updated_at,
                    COUNT(b.timestamp) AS bar_count,
                    MIN(b.timestamp) AS first_bar,
                    MAX(b.timestamp) AS last_bar
                FROM coverage AS c
                LEFT JOIN bars AS b
                  ON b.symbol=c.symbol
                 AND b.timeframe=c.timeframe
                 AND b.feed=c.feed
                 AND b.adjustment=c.adjustment
                GROUP BY
                    c.symbol, c.timeframe, c.feed, c.adjustment,
                    c.requested_start, c.requested_end, c.updated_at
                ORDER BY c.symbol, c.timeframe, c.feed, c.adjustment
                """
            ).fetchall()
        return [dict(row) for row in rows]
