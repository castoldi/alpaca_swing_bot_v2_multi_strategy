"""Historical adjusted-price cache with atomic full-range generations."""
from __future__ import annotations

import sqlite3
import hashlib
import os
import sys
from contextlib import closing
from functools import lru_cache
from uuid import uuid4
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import pandas as pd
import numpy as np

import data_feed
import yfinance_history
from logger_setup import get_logger


ROOT = Path(__file__).parent
CACHE_DB = ROOT / "cache" / "market_data.db"
OHLCV = ["open", "high", "low", "close", "volume"]
# Partitions filled only by an explicit import script (the provider needs a
# running IB Gateway). A read never re-fetches them just because a new UTC day
# started; it serves whatever the last import stored.
IMPORT_ONLY_FEEDS = {"ibkr"}

SeriesKey = tuple[str, str, str, str]
Fetcher = Callable[..., pd.DataFrame]
log = get_logger(__name__)


@lru_cache(maxsize=4)
def _read_run_id(pid: int) -> str:
    """Group reads across cache instances in one research process."""
    return uuid4().hex


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
        self.fetcher = fetcher or self._default_fetcher
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @staticmethod
    def _default_fetcher(
        ticker: str, start, end, timeframe: str, *, feed: str, strict: bool = False
    ) -> pd.DataFrame:
        """Route by feed: "yfinance" is a cache partition, not an Alpaca feed."""
        if feed in IMPORT_ONLY_FEEDS:
            raise ValueError(
                f"The {feed!r} cache partition is import-only and does not cover this "
                f"range: run scripts/import_{feed}_history.py"
            )
        if feed == "yfinance":
            return yfinance_history.fetch_bars(ticker, start, end, timeframe, strict=strict)
        return data_feed.fetch_bars(ticker, start, end, timeframe, feed=feed, strict=strict)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=120)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as connection, connection:
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
                CREATE TABLE IF NOT EXISTS cache_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
                    feed TEXT NOT NULL, adjustment TEXT NOT NULL,
                    requested_start TEXT NOT NULL, requested_end TEXT NOT NULL,
                    fetched_at TEXT NOT NULL, content_sha256 TEXT NOT NULL,
                    hash_format TEXT NOT NULL, bar_count INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cache_reads (
                    id INTEGER PRIMARY KEY,
                    run_id TEXT NOT NULL, pid INTEGER NOT NULL, consumer TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL, read_at TEXT NOT NULL,
                    requested_start TEXT NOT NULL, requested_end TEXT NOT NULL
                );
                """
            )
            connection.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(coverage)')}
            if 'snapshot_id' not in columns:
                connection.execute('ALTER TABLE coverage ADD COLUMN snapshot_id TEXT')

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
        if index.hasnans:
            raise ValueError('Historical bars contain missing timestamps')
        normalized.index = index.as_unit('ns')
        normalized.index.name = "timestamp"
        values = normalized.to_numpy()
        if not np.isfinite(values).all():
            raise ValueError('Historical bars contain nonfinite values')
        if (normalized[['open', 'high', 'low', 'close']] <= 0).any().any() or (normalized.volume < 0).any():
            raise ValueError('Historical bars contain invalid prices or volume')
        return normalized[~normalized.index.duplicated(keep="last")].sort_index()

    def _replace_snapshot(self, connection, key, start, end, frame, now):
        normalized = self._normalize_frame(frame)
        normalized = normalized[(normalized.index >= start) & (normalized.index < end)]
        if normalized.empty:
            previous = connection.execute(
                'SELECT 1 FROM bars WHERE symbol=? AND timeframe=? AND feed=? AND adjustment=? LIMIT 1',
                key,
            ).fetchone()
            if previous:
                raise ValueError('Empty refresh would erase an existing populated price history')
        digest = hashlib.sha256(
            pd.util.hash_pandas_object(normalized, index=True).to_numpy(dtype='<u8').tobytes()
        ).hexdigest()
        snapshot = dict(
            snapshot_id=uuid4().hex, symbol=key[0], timeframe=key[1], feed=key[2],
            adjustment=key[3], requested_start=start.isoformat(), requested_end=end.isoformat(),
            fetched_at=now.isoformat(), content_sha256=digest,
            hash_format=f'pandas-{pd.__version__}-hash_pandas_object-float64-utc-ns-sha256',
            bar_count=len(normalized),
        )
        # Replacement, not upsert: withdrawn provider rows must disappear too.
        connection.execute('DELETE FROM bars WHERE symbol=? AND timeframe=? AND feed=? AND adjustment=?', key)
        connection.executemany(
            'INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            ((*key, pd.Timestamp(row[0]).isoformat(), *row[1:])
             for row in normalized.itertuples(name=None)),
        )
        connection.execute(
            '''INSERT INTO cache_snapshots VALUES (
               :snapshot_id, :symbol, :timeframe, :feed, :adjustment,
               :requested_start, :requested_end, :fetched_at, :content_sha256,
               :hash_format, :bar_count)''', snapshot,
        )
        connection.execute(
            '''INSERT INTO coverage (symbol, timeframe, feed, adjustment,
               requested_start, requested_end, updated_at, snapshot_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (symbol, timeframe, feed, adjustment) DO UPDATE SET
               requested_start=excluded.requested_start, requested_end=excluded.requested_end,
               updated_at=excluded.updated_at, snapshot_id=excluded.snapshot_id''',
            (*key, start.isoformat(), end.isoformat(), now.isoformat(), snapshot['snapshot_id']),
        )
        return snapshot

    @staticmethod
    def _read(connection, key, start, end):
        rows = connection.execute(
            '''SELECT timestamp, open, high, low, close, volume FROM bars
               WHERE symbol=? AND timeframe=? AND feed=? AND adjustment=?
               AND timestamp>=? AND timestamp<? ORDER BY timestamp''',
            (*key, start.isoformat(), end.isoformat()),
        ).fetchall()
        if not rows:
            return _empty_frame()
        frame = pd.DataFrame([dict(row) for row in rows])
        frame.index = pd.to_datetime(frame.pop('timestamp')).astype('datetime64[ns]')
        frame.index.name = 'timestamp'
        return frame[OHLCV]

    def get_bars(
        self, ticker: str, start: date | datetime, end: date | datetime,
        timeframe: str, *, feed: str | None = None, adjustment: str = 'all',
        refresh: bool = False,
    ) -> pd.DataFrame:
        """Return one adjusted-price generation for [start, end).

        An extension, explicit refresh, new UTC day, or unversioned legacy cache
        replaces the entire covered union. Provider failures never advance
        coverage or return stale data as refreshed. Snapshot fingerprints and
        per-process read manifests are retained; old bar generations are not.
        """
        feed = data_feed.resolve_feed() if feed is None else feed.strip().lower()
        key = (ticker.upper(), timeframe.lower(), feed, adjustment.lower())
        if key[2] not in {'iex', 'sip', 'yfinance', *IMPORT_ONLY_FEEDS}:
            raise ValueError(f'Unsupported stock feed: {feed}')
        if key[3] != 'all':
            raise ValueError("Only adjustment='all' is supported by this cache")
        now = _timestamp(self.now_fn())
        start_ts = _timestamp(start)
        end_ts = min(_timestamp(end), _completed_data_ceiling(self.now_fn()))
        if end_ts <= start_ts:
            return _empty_frame()

        # Serialize refresh decisions, provider calls, replacement and the
        # returned read. WAL readers can still see the preceding committed
        # generation; competing writers cannot publish out-of-order segments.
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            coverage = connection.execute(
                'SELECT * FROM coverage WHERE symbol=? AND timeframe=? AND feed=? AND adjustment=?', key,
            ).fetchone()
            snapshot = None
            full_start, full_end = start_ts, end_ts
            if coverage:
                full_start = min(start_ts, pd.Timestamp(coverage['requested_start']))
                full_end = max(end_ts, pd.Timestamp(coverage['requested_end']))
                row = connection.execute('SELECT * FROM cache_snapshots WHERE snapshot_id=?',
                                         (coverage['snapshot_id'],)).fetchone()
                snapshot = dict(row) if row else None
            if key[2] in IMPORT_ONLY_FEEDS and snapshot is not None and not refresh:
                # Serve the imported generation as-is; asking past its edges
                # returns the covered part rather than calling the provider.
                needs_refresh = False
            else:
                needs_refresh = (
                    refresh or snapshot is None
                    or pd.Timestamp(snapshot['fetched_at']).normalize() != now.normalize()
                    or full_start < pd.Timestamp(snapshot['requested_start'])
                    or full_end > pd.Timestamp(snapshot['requested_end'])
                )
            if needs_refresh:
                frame = self.fetcher(key[0], full_start.to_pydatetime(), full_end.to_pydatetime(),
                                     key[1], feed=key[2], strict=True)
                snapshot = self._replace_snapshot(connection, key, full_start, full_end, frame, now)
            result = self._read(connection, key, start_ts, end_ts)
            run_id = _read_run_id(os.getpid())
            connection.execute(
                '''INSERT INTO cache_reads (run_id, pid, consumer, snapshot_id, read_at,
                   requested_start, requested_end) VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (run_id, os.getpid(), Path(sys.argv[0]).name, snapshot['snapshot_id'],
                 now.isoformat(), start_ts.isoformat(), end_ts.isoformat()),
            )
            result.attrs.update(timeframe=key[1], feed=key[2], adjustment=key[3],
                                cache_snapshot=snapshot, cache_read_run_id=run_id)
            log.info('Historical data snapshot: run=%s snapshot=%s %s/%s/%s [%s, %s)',
                     run_id, snapshot['snapshot_id'], key[0], key[1], key[2], start_ts, end_ts)
            return result

    def read_manifest(self, run_id: str | None = None) -> list[dict]:
        """Exact read ranges and immutable fingerprints for one research process.

        The run ID is returned on frames and logged. Old price rows are not
        retained: this is an audit manifest, not an offline snapshot archive.
        """
        with closing(self._connect()) as connection:
            rows = connection.execute(
                '''SELECT r.id, r.run_id, r.pid, r.consumer, r.read_at,
                   r.requested_start AS read_start, r.requested_end AS read_end, s.*
                   FROM cache_reads r JOIN cache_snapshots s ON s.snapshot_id=r.snapshot_id
                   WHERE r.run_id=? ORDER BY r.id''',
                (run_id or _read_run_id(os.getpid()),),
            ).fetchall()
        return [dict(row) for row in rows]


    def status(self) -> list[dict]:
        """Describe every cached series and its successful request coverage."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT
                    c.symbol, c.timeframe, c.feed, c.adjustment,
                    c.requested_start, c.requested_end, c.updated_at, c.snapshot_id,
                    s.content_sha256, s.hash_format,
                    COUNT(b.timestamp) AS bar_count,
                    MIN(b.timestamp) AS first_bar,
                    MAX(b.timestamp) AS last_bar
                FROM coverage AS c
                LEFT JOIN cache_snapshots AS s ON s.snapshot_id=c.snapshot_id
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
