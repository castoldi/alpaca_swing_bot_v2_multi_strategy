"""Regular-session minute execution, separate from strategy signal candles.

Minute opens are executable at their timestamp; intraminute barrier touches
are dated at minute end so their proceeds cannot finance an earlier fill.
Unknown paths touching both barriers use stop first. This models full fills,
not queue position, fees, spread, or broker latency.
"""
from __future__ import annotations

from dataclasses import replace
from functools import lru_cache

import exchange_calendars as xcals
import numpy as np
import pandas as pd

from strategies.base import ExitLeg, TP_SPLITS, _max_holding_days, is_tp_reachable_in_days


def utc(value):
    ts = pd.Timestamp(value)
    return ts.tz_convert('UTC').tz_localize(None) if ts.tzinfo else ts


@lru_cache(maxsize=16)
def _calendar(first_year, last_year):
    return xcals.get_calendar('XNYS', start=f'{first_year}-01-01',
                              end=f'{last_year}-12-31')


def regular_minutes(frame):
    """Accept only complete one-minute OHLCV bars within XNYS core hours.

    The schedule includes DST, holidays, extraordinary closures and early
    closes. A bar starting at the session close is already extended-hours.
    """
    if frame.empty:
        raise ValueError('Missing one-minute execution data')
    out = frame.copy()
    index = pd.to_datetime(out.index, utc=True).tz_localize(None)
    if not index.is_monotonic_increasing or index.has_duplicates:
        raise ValueError('Execution bars must be sorted and unique')
    if not (index == index.floor('min')).all():
        raise ValueError('Execution timestamps must label one-minute bar starts')
    out.index = index
    schedule = _calendar(index[0].year, index[-1].year).schedule
    opens = pd.DatetimeIndex(schedule['open']).tz_localize(None)
    closes = pd.DatetimeIndex(schedule['close']).tz_localize(None)
    positions = opens.searchsorted(index, side='right') - 1
    mask = (positions >= 0) & (index < closes.take(np.maximum(positions, 0)))
    out = out.loc[mask & (out['volume'].to_numpy() > 0)].copy()
    values = out[['open', 'high', 'low', 'close']].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError('Invalid execution OHLC prices')
    if ((out['low'] > out[['open', 'close']].min(axis=1)).any()
            or (out['high'] < out[['open', 'close']].max(axis=1)).any()):
        raise ValueError('Inconsistent execution OHLC prices')
    return out


def signal_availability(index, timeframe):
    index = pd.to_datetime(index, utc=True)
    if timeframe == '4h':
        return (index + pd.Timedelta(hours=4)).tz_localize(None)
    if timeframe == '1d':
        # Alpaca's daily aggregation can include extended-hours trades. Treat
        # the whole New York day as complete before using its final OHLC.
        return (index.tz_convert('America/New_York').normalize()
                + pd.DateOffset(days=1)).tz_convert('UTC').tz_localize(None)
    raise ValueError(f'Unsupported signal timeframe: {timeframe}')


def load_execution_bars(frame, ticker, start, end):
    """Use the same feed/adjustment as the signal series; never coarse fallback."""
    feed = frame.attrs.get('feed')
    if feed not in {'sip', 'iex'} or frame.attrs.get('adjustment') != 'all':
        raise ValueError('Provide one-minute execution_bars for custom signal data; '
                         'automatic execution requires Alpaca feed/adjustment metadata')
    from market_cache import MarketDataCache
    return MarketDataCache().get_bars(ticker, utc(start), utc(end) + pd.Timedelta(nanoseconds=1),
                                      '1min', feed=feed, adjustment='all')


def validate_execution_provenance(signal_frame, execution_bars):
    """Reject execution bars whose own metadata contradicts one-minute, same-source data.

    Applies to caller-supplied ``execution_bars`` as well as auto-loaded ones. A
    frame's ``attrs`` are the only signal we have that it is actually 4h/daily
    data mislabeled as execution bars, or drawn from a different feed/adjustment
    than the signal series it will be priced against. Missing attrs (the common
    case for synthetic/legacy frames) are not checked — only an explicit conflict
    is rejected.
    """
    timeframe = execution_bars.attrs.get('timeframe')
    if timeframe is not None and timeframe != '1min':
        raise ValueError(f'execution_bars must be one-minute bars, got timeframe={timeframe!r}')
    feed = execution_bars.attrs.get('feed')
    signal_feed = signal_frame.attrs.get('feed')
    if feed is not None and signal_feed is not None and feed != signal_feed:
        raise ValueError(f'execution_bars feed {feed!r} does not match signal feed {signal_feed!r}')
    adjustment = execution_bars.attrs.get('adjustment')
    signal_adjustment = signal_frame.attrs.get('adjustment')
    if adjustment is not None and signal_adjustment is not None and adjustment != signal_adjustment:
        raise ValueError(
            f'execution_bars adjustment {adjustment!r} does not match signal adjustment {signal_adjustment!r}'
        )


def _exits(rows, entry_idx, signal, known, signal_closes, exit_reasons,
           params, strategy, scaled=False):
    """Run one full-position bracket or the research scale-out on the same clock."""
    stop = signal.stop_loss
    targets = [signal.tp1, signal.tp2, signal.tp3] if scaled else [signal.tp3]
    fractions = list(TP_SPLITS) if scaled else [1.0]
    if strategy.exit_mode == 'signal_with_stop':
        targets = []
    hit = [False] * len(targets)
    remaining = 1.0
    legs = []
    baseline = known[entry_idx]
    cursor = baseline
    pending = None
    max_bars = _max_holding_days(signal, params)

    for j in range(entry_idx, len(rows)):
        bar = rows[j]
        timestamp, opening, high, low, close = bar
        held = max(0, int(known[j] - baseline))
        for k in range(cursor + 1, known[j] + 1):
            if strategy.exit_mode == 'signal_with_stop':
                pending = pending or exit_reasons[k]
            elif k - baseline >= max_bars and signal_closes[k] >= signal.entry_price:
                pending = pending or 'time_stop'
        cursor = known[j]

        # An opening event has known precedence over the rest of the minute.
        if opening <= stop:
            reason = 'gap_stop' if opening < stop else 'stop_loss'
            legs.append(ExitLeg(timestamp, opening, reason, held, remaining))
            return legs
        for k, target in enumerate(targets):
            if not hit[k] and opening >= target:
                hit[k] = True
                legs.append(ExitLeg(timestamp, opening, f'tp{k+1}' if scaled else 'take_profit',
                                    held, fractions[k]))
                remaining -= fractions[k]
        if remaining < 1e-9:
            return legs
        if pending:
            legs.append(ExitLeg(timestamp, opening, pending, held, remaining))
            return legs

        event_time = timestamp + pd.Timedelta(minutes=1)
        # For the remaining intraminute path we cannot know high/low ordering.
        if low <= stop:
            legs.append(ExitLeg(event_time, stop, 'stop_loss', held, remaining))
            return legs
        for k, target in enumerate(targets):
            if not hit[k] and high >= target:
                hit[k] = True
                legs.append(ExitLeg(event_time, target, f'tp{k+1}' if scaled else 'take_profit',
                                    held, fractions[k]))
                remaining -= fractions[k]
        if remaining < 1e-9:
            return legs
        # Research stop ratchets become effective next minute, as in the
        # prior scale-out model; never assume a favorable intrabar path.
        if scaled and hit[1]:
            stop = max(stop, signal.tp1)
        elif scaled and hit[0]:
            stop = max(stop, signal.entry_price)

    legs.append(ExitLeg(rows[-1][0] + pd.Timedelta(minutes=1), rows[-1][4],
                        'end_of_data', max(0, int(known[-1] - baseline)), remaining))
    return legs


def collect_session_candidates(data, ticker, start, end, params, strategy, execution_bars):
    from backtest_portfolio import BacktestCandidate

    timeframe = data.attrs.get('timeframe', strategy.timeframe)
    available = signal_availability(data.index, timeframe)
    minutes = regular_minutes(execution_bars)
    # Never borrow a minute's close/high/low across the requested boundary.
    minutes = minutes[(minutes.index >= utc(start))
                      & (minutes.index + pd.Timedelta(minutes=1) <= utc(end))]
    if minutes.empty:
        return []
    rows = list(minutes[['open', 'high', 'low', 'close']].itertuples(name=None))
    known = available.searchsorted(minutes.index, side='right') - 1
    closes = data['close'].to_numpy()
    exit_reasons = [None] * len(data)
    if strategy.exit_mode == 'signal_with_stop':
        for idx in range(len(data)):
            exit_reasons[idx] = strategy.check_exit(data, idx, params)
    candidates = []
    for idx, timestamp in enumerate(data.index):
        if not utc(start) <= utc(timestamp) <= utc(end):
            continue
        signal = strategy.check_entry(data, idx, params)
        if signal is None:
            continue
        fill_idx = minutes.index.searchsorted(available[idx])
        if fill_idx >= len(minutes):
            continue
        fill_date, fill_price = rows[fill_idx][:2]
        if strategy.exit_mode != 'signal_with_stop':
            if not is_tp_reachable_in_days(signal.entry_price, signal.tp1, signal.atr, days=4):
                continue
            if signal.entry_price <= 0 or abs(fill_price - signal.entry_price) / signal.entry_price > params.entry_max_slippage_pct:
                continue
        stop = (fill_price * (1 - strategy.stop_loss_fraction(params))
                if strategy.exit_mode == 'signal_with_stop' else signal.stop_loss)
        fill_signal = replace(signal, date=fill_date, entry_price=fill_price, stop_loss=stop)
        single = tuple(_exits(rows, fill_idx, fill_signal, known, closes, exit_reasons,
                              params, strategy))
        scaled = (tuple(_exits(rows, fill_idx, fill_signal, known, closes, exit_reasons,
                               params, strategy, scaled=True))
                  if strategy.exit_mode != 'signal_with_stop' else single)
        candidates.append(BacktestCandidate(
            ticker, fill_date, fill_price, stop,
            signal.tp3 if strategy.exit_mode != 'signal_with_stop' else 0.0,
            strategy.name, single, scaled, pd.Timestamp(signal.date), available[idx],
        ))
    return candidates
