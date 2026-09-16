"""Time-stop eligibility in XNYS session closes, shared by live and research.

The first close strictly after a fill counts as session one, even for a partial
entry session. This is an eligibility threshold, not a forced maximum lifetime.
"""
from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd


def utc(value):
    """Naive timestamps in project data represent UTC."""
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        raise ValueError('Missing holding-period timestamp')
    return ts.tz_convert('UTC').tz_localize(None) if ts.tzinfo else ts


@lru_cache(maxsize=16)
def calendar(first_year, last_year):
    return xcals.get_calendar('XNYS', start=f'{first_year}-01-01',
                              end=f'{last_year}-12-31')


def holding_sessions_limit(strategy, params):
    for prefix, field in (
        ('breakout', 'breakout_max_holding_days'),
        ('mean_reversion', 'mr_max_holding_days'),
        ('momentum_macd', 'macd_max_holding_days'),
        ('ensemble', 'ensemble_max_holding_days'),
    ):
        if strategy.startswith(prefix):
            return getattr(params, field)
    return params.max_holding_days  # trend pullback, regime, synthetic strategies


@lru_cache(maxsize=4096)
def holding_deadline(fill_at, sessions):
    """UTC close of the Nth session strictly after the actual fill."""
    fill = utc(fill_at)
    if isinstance(sessions, bool) or int(sessions) != sessions or sessions < 1:
        raise ValueError('Holding sessions must be a positive integer')
    sessions = int(sessions)
    schedule = calendar(fill.year, fill.year + sessions // 200 + 1).schedule
    closes = pd.DatetimeIndex(schedule['close']).tz_localize(None)
    return closes[closes.searchsorted(fill, side='right') + sessions - 1]
