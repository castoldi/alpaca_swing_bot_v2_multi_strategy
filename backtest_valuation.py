"""Observable regular-session prices for the portfolio daily-loss guard."""
from __future__ import annotations

import numpy as np
import pandas as pd

from backtest_execution import (
    _calendar, load_execution_bars, regular_minutes, utc,
    validate_execution_provenance,
)


def session_date(timestamp):
    return utc(timestamp).tz_localize('UTC').tz_convert('America/New_York').date()


def previous_session_close(timestamp):
    """Last exchange close before this New York date, including early closes."""
    day = pd.Timestamp(session_date(timestamp))
    schedule = _calendar(day.year - 1, day.year).schedule
    closes = pd.DatetimeIndex(schedule['close']).tz_localize(None)
    return closes[closes.searchsorted(day, side='left') - 1]


def prepare_valuation_frames(frames, start, end):
    """Convert bar starts into price-observation times once per portfolio run.

    Production signal frames load matching minute data through the same cache
    as execution. Custom callers may pass minute OHLCV with timeframe='1min',
    or an explicit close-only observation series (price_timestamps='observed').
    Unlabeled closes are ambiguous and must never be assumed already known.
    """
    result = {}
    for ticker, source in frames.items():
        if source.attrs.get('price_timestamps') == 'observed':
            if set(source.columns) != {'close'}:
                raise ValueError(f'{ticker}: observed prices must be close-only')
            out = source.copy()
            out.index = pd.to_datetime(out.index, utc=True).tz_localize(None)
        else:
            timeframe = source.attrs.get('timeframe')
            if timeframe == '1min':
                minutes = source
            elif timeframe in {'1d', '4h'}:
                minutes = load_execution_bars(
                    source, ticker, previous_session_close(start) - pd.Timedelta(minutes=1),
                    utc(end) + pd.Timedelta(minutes=1),
                )
            else:
                raise ValueError(f'{ticker}: valuation needs minute OHLCV or explicit observed prices')
            validate_execution_provenance(source if timeframe != '1min' else minutes, minutes)
            minutes = regular_minutes(minutes)
            # A minute's close is known at its end. At a shared boundary the
            # next open is newer than the preceding close and takes precedence.
            closed = minutes['close'].copy()
            closed.index = closed.index + pd.Timedelta(minutes=1)
            marks = pd.concat([closed, minutes['open']]).sort_index(kind='stable')
            marks = marks[~marks.index.duplicated(keep='last')]
            out = marks.rename('close').to_frame()
        if not out.index.is_monotonic_increasing or out.index.has_duplicates:
            raise ValueError(f'{ticker}: observed prices must be sorted and unique')
        values = out['close'].to_numpy(dtype=float)
        if not np.isfinite(values).all() or (values <= 0).any():
            raise ValueError(f'{ticker}: invalid valuation prices')
        out.attrs['price_timestamps'] = 'observed'
        result[ticker] = out
    return result
