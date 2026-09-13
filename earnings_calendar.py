"""Observed earnings schedules and a shared session-based entry policy.

Snapshots are append-only: an event downloaded today is never evidence that
its date was known yesterday. Unknown/stale calendars suppress earnings-aware
signals. All stored observation timestamps are UTC; event dates use New York.
"""
from bisect import bisect_right
from functools import lru_cache
import json
from pathlib import Path
import sqlite3

import exchange_calendars as xcals
import pandas as pd

from logger_setup import get_logger

log = get_logger(__name__)
_STORE = None
SUCCESS_TTL = pd.Timedelta(hours=6)
RETRY_TTL = pd.Timedelta(minutes=5)


def utc(value):
    ts = pd.Timestamp(value)
    return ts.tz_localize('UTC') if ts.tzinfo is None else ts.tz_convert('UTC')


def fetch_events(ticker):
    """Current Yahoo schedule, never a source of historical observation dates."""
    import yfinance as yf
    from yfinance.data import YfData
    # yfinance shares a process-wide HTTP LRU without a TTL. A new Ticker
    # alone would re-stamp the same cached response as a fresh observation.
    # If this interface changes, refresh fails closed instead of claiming freshness.
    YfData().cache_get.cache_clear()
    rows = yf.Ticker(ticker).get_earnings_dates(limit=16)
    if rows is None or rows.empty:
        return []
    if 'Event Type' in rows:
        rows = rows[rows['Event Type'].astype(str).str.lower().isin(['earnings', '2'])]
    events = []
    for value in rows.index:
        timestamp = pd.Timestamp(value)
        if pd.isna(timestamp):
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize('America/New_York')
        local = timestamp.tz_convert('America/New_York')
        # Midnight is generally a date-only placeholder; do not assume BMO.
        events.append(local.date().isoformat() if local.time().isoformat() == '00:00:00'
                      else local.isoformat())
    return sorted(set(events))


class CalendarStore:
    def __init__(self, path=None, fetcher=None):
        self.path = Path(path or Path(__file__).parent / 'cache' / 'earnings.db')
        self.fetcher = fetcher or fetch_events
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as con:
            con.execute('''CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, observed_at TEXT NOT NULL,
                valid_until TEXT NOT NULL, events TEXT NOT NULL, status TEXT NOT NULL,
                source TEXT NOT NULL)''')
            con.execute('CREATE INDEX IF NOT EXISTS snapshot_lookup ON snapshots(ticker, observed_at)')

    def record(self, ticker, observed_at, events, *, valid_until=None, source, status='ok'):
        """Import a genuinely observed schedule; never backdate current data.

        Empty successful imports explicitly certify no scheduled events during
        their validity window. Automatic empty provider responses use unknown.
        """
        observed = utc(observed_at)
        until = utc(valid_until) if valid_until is not None else observed + SUCCESS_TTL
        if pd.isna(observed) or pd.isna(until) or until <= observed:
            raise ValueError('Snapshot validity must end after observation')
        if status not in {'ok', 'unknown'} or not source:
            raise ValueError('Snapshot requires status and source provenance')
        values = [str(event) for event in events]
        for event in values:
            _event_window(event, 0)  # Validate before persisting.
        with sqlite3.connect(self.path) as con:
            con.execute('INSERT INTO snapshots(ticker, observed_at, valid_until, events, status, source) '
                        'VALUES (?,?,?,?,?,?)', (ticker.upper(), observed.isoformat(), until.isoformat(),
                                                json.dumps(values), status, source))

    def history(self, ticker, as_of):
        with sqlite3.connect(self.path) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute('SELECT * FROM snapshots WHERE ticker=? AND observed_at<=? '
                               'ORDER BY observed_at, id', (ticker.upper(), utc(as_of).isoformat())).fetchall()
        return [dict(row, events=json.loads(row['events'])) for row in rows]

    def refresh(self, ticker, as_of=None):
        now = utc(as_of) if as_of is not None else pd.Timestamp.now(tz='UTC')
        history = self.history(ticker, now)
        if history and now < utc(history[-1]['valid_until']):
            return
        try:
            events = self.fetcher(ticker)
            # An empty/past-only listing is not evidence of no upcoming event.
            upcoming = any(_event_window(str(event), 0)[1] > now for event in events)
            status = 'ok' if upcoming else 'unknown'
        except Exception as exc:
            log.warning('%s: earnings refresh failed (%s); entries need known data', ticker, type(exc).__name__)
            events, status = [], 'unknown'
        observed = now if as_of is not None else pd.Timestamp.now(tz='UTC')
        self.record(ticker, observed, events,
                    valid_until=observed + (SUCCESS_TTL if status == 'ok' else RETRY_TTL),
                    source='yfinance current schedule', status=status)


def get_store():
    global _STORE
    if _STORE is None:
        _STORE = CalendarStore()
    return _STORE


@lru_cache(maxsize=64)
def _calendar(year):
    return xcals.get_calendar('XNYS', start=f'{year-1}-01-01', end=f'{year+1}-12-31')


@lru_cache(maxsize=4096)
def _event_window(event, days):
    timestamp = pd.Timestamp(event)
    if pd.isna(timestamp):
        raise ValueError('Invalid earnings date')
    local = (timestamp.tz_localize('America/New_York') if timestamp.tzinfo is None
             else timestamp.tz_convert('America/New_York'))
    unknown_time = local.time().isoformat() == '00:00:00'
    schedule = _calendar(local.year).schedule
    event_date = pd.Timestamp(local.date())
    session_index = schedule.index.searchsorted(event_date)
    first_date = schedule.index[max(0, session_index - days)]
    start = first_date.tz_localize('America/New_York').tz_convert('UTC')
    if unknown_time:
        end = (local.normalize() + pd.DateOffset(days=1)).tz_convert('UTC')
    else:
        # A pre-open/weekend event has passed by the next regular opening.
        end = max(local.tz_convert('UTC'), utc(schedule.iloc[session_index]['open']))
    return start, end


def apply_filter(df, ticker, params, *, decision_times=None, live=False, as_of=None, store=None):
    out = df.copy()
    if out.empty or params.earnings_avoid_days <= 0:
        out['near_earnings'] = False
        out['earnings_status'] = 'disabled'
        return out
    store = store or get_store()
    if live:
        store.refresh(ticker, as_of)
        now = utc(as_of) if as_of is not None else pd.Timestamp.now(tz='UTC')
        decisions = [now] * len(out)
    else:
        if decision_times is None:
            from backtest_execution import signal_availability
            from config import BAR_TIMEFRAME
            decision_times = signal_availability(out.index, out.attrs.get('timeframe', BAR_TIMEFRAME))
        decisions = [utc(t) if not pd.isna(t) else pd.NaT for t in decision_times]
        if len(decisions) != len(out):
            raise ValueError('One earnings decision time is required per signal bar')
    valid = [t for t in decisions if not pd.isna(t)]
    history = store.history(ticker, max(valid)) if valid else []
    observed = [utc(row['observed_at']) for row in history]
    statuses = []
    for decision in decisions:
        position = bisect_right(observed, decision) - 1 if not pd.isna(decision) else -1
        snapshot = history[position] if position >= 0 else None
        if (snapshot is None or snapshot['status'] != 'ok'
                or decision >= utc(snapshot['valid_until'])):
            statuses.append('unknown')
            continue
        blocked = any(start <= decision < end for start, end in (
            _event_window(event, params.earnings_avoid_days) for event in snapshot['events']))
        statuses.append('blocked' if blocked else 'clear')
    out['earnings_status'] = statuses
    out['near_earnings'] = out['earnings_status'] != 'clear'
    if 'unknown' in statuses:
        log.warning('%s: unknown/stale earnings calendar for %d/%d decisions; Trend Pullback suppressed',
                    ticker, statuses.count('unknown'), len(statuses))
    return out
