from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest

from config import PARAMS


@pytest.mark.parametrize('fill,sessions,expected', [
    ('2026-03-06 15:00Z', 2, '2026-03-09 20:00'),  # weekend + DST
    ('2025-11-26 20:00Z', 2, '2025-11-28 18:00'),  # Thanksgiving + early close
    ('2025-11-28 18:00Z', 1, '2025-12-01 21:00'),  # exact close does not count
    ('2025-11-28 17:59:59Z', 1, '2025-11-28 18:00'),
    ('2025-07-03 18:00Z', 1, '2025-07-07 20:00'),  # after early close + holiday
    ('2026-12-31 15:00Z', 2, '2027-01-04 21:00'),
])
def test_deadline_counts_exchange_closes_strictly_after_fill(fill, sessions, expected):
    from holding_period import holding_deadline
    assert holding_deadline(fill, sessions) == pd.Timestamp(expected)


def test_live_threshold_is_exact_and_uses_actual_fill(monkeypatch):
    import bot
    monkeypatch.setattr(bot, 'PARAMS', replace(PARAMS, ensemble_max_holding_days=2))
    trade = dict(strategy='ensemble', entry_filled_at='2025-11-26T20:00:00Z',
                 created_at='2025-11-01', entry_date='2025-10-01')
    assert not bot._time_stop_due(trade, as_of='2025-11-28T17:59:59Z')
    assert bot._time_stop_due(trade, as_of='2025-11-28T18:00:00Z')
    trade['created_at'] = '2020-01-01'
    assert not bot._time_stop_due(trade, as_of='2025-11-28T17:59:59Z')


@pytest.mark.parametrize('fill', [None, '', 'not-a-time', 'NaT', '2030-01-01'])
def test_missing_invalid_or_future_fill_does_not_trigger_time_exit(fill):
    import bot
    assert not bot._time_stop_due(dict(strategy='ensemble', entry_filled_at=fill,
                                      created_at='2000-01-01', entry_date='2000-01-01'),
                                  as_of='2026-03-09 20:00Z')


def test_fill_timestamp_is_persisted_and_not_erased_by_older_call():
    from dashboard import db
    trade_id = db.save_trade('AMD', 'ensemble', '2025-11-01', 100, 90, 110, shares=2)
    db.set_entry_fill(trade_id, 101., 2., '2025-11-26T20:00:00+00:00')
    db.set_entry_fill(trade_id, 101., 2.)
    assert db.get_open_trade('AMD', 'ensemble')['entry_filled_at'] == '2025-11-26T20:00:00+00:00'


def test_record_fill_uses_broker_timestamp():
    import bot
    from dashboard import db
    trade_id = db.save_trade('AMD', 'ensemble', '2025-11-01', 100, 90, 110, shares=2)
    order = SimpleNamespace(status='filled', filled_avg_price='101', filled_qty='2',
                            filled_at=pd.Timestamp('2025-11-26 20:00Z'))
    bot._record_entry_fill(SimpleNamespace(get_order_by_id=lambda _: order), trade_id, 'entry')
    assert db.get_open_trade('AMD', 'ensemble')['entry_filled_at'] == '2025-11-26T20:00:00+00:00'


@pytest.mark.parametrize('strategy,expected', [
    ('trend_pullback', 5), ('regime', 5), ('breakout', 7),
    ('mean_reversion', 3), ('momentum_macd', 6), ('ensemble', 6),
])
def test_strategy_limits_remain_unchanged(strategy, expected):
    from holding_period import holding_sessions_limit
    assert holding_sessions_limit(strategy, PARAMS) == expected


def test_legacy_schema_migration_leaves_missing_fill_time_unknown():
    from dashboard import db
    trade_id = db.save_trade('AMD', 'ensemble', '2025-11-01', 100, 90, 110, shares=2)
    with db._con() as connection:
        connection.execute('ALTER TABLE trades DROP COLUMN entry_filled_at')
    db.init_db()
    db.init_db()
    assert db.get_open_trade('AMD', 'ensemble')['entry_filled_at'] is None


@pytest.mark.parametrize('scaled', [False, True])
def test_coarse_open_fill_clock_ignores_stale_signal_date(scaled):
    from strategies.base import EntrySignal, simulate_exit, simulate_exit_scaleout
    frame = pd.DataFrame(dict(open=[100., 100.], high=[101., 101.],
                              low=[99., 99.], close=[100., 100.]),
                         index=pd.to_datetime(['2025-11-28 14:30', '2025-11-28 15:30']))
    frame.attrs['timeframe'] = '4h'
    signal = EntrySignal(pd.Timestamp('2025-10-01'), 100., 90., 130., 10., 55.)
    params = replace(PARAMS, max_holding_days=2)
    if scaled:
        reason = simulate_exit_scaleout(frame, 0, signal, params, entry_at_open=True)[-1].reason
    else:
        reason = simulate_exit(frame, 0, signal, params, entry_at_open=True)[2]
    assert reason == 'end_of_data'
