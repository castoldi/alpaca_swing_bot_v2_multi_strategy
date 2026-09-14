"""F07: hand-calculated portfolio losses on the regular-session event clock."""
import pandas as pd
import pytest

from backtest_portfolio import run_annual_portfolio
from tests.test_kill_switch import _single_candidate


def minutes(times, opens, closes=None):
    closes = opens if closes is None else closes
    frame = pd.DataFrame({
        'open': opens, 'close': closes,
        'high': [max(o, c) for o, c in zip(opens, closes)],
        'low': [min(o, c) for o, c in zip(opens, closes)],
        'volume': 1000,
    }, index=pd.to_datetime(times))
    frame.attrs['timeframe'] = '1min'
    return frame


def run(candidates, frame, **kwargs):
    return run_annual_portfolio(
        candidates, initial_equity=1000, position_fraction=.5,
        max_positions=5, price_frames={'HELD': frame}, apply_tax=False,
        max_daily_loss_pct=.03, **kwargs,
    )


def held(exit_date='2026-01-09 20:00', exit_price=100):
    return _single_candidate('HELD', '2026-01-05 15:00', 100, exit_date, exit_price)


def attempt(timestamp, ticker='NEW'):
    # Closes before any subsequent attempt, so its valuation is never needed.
    return _single_candidate(ticker, timestamp, 50,
                             pd.Timestamp(timestamp) + pd.Timedelta(minutes=1), 50)


def test_realized_loss_before_first_candidate_is_not_erased():
    result = run([held('2026-01-06 14:31', 90), attempt('2026-01-06 15:00')],
                 minutes(['2026-01-05 20:59', '2026-01-06 14:30'], [100, 90]))
    assert result.accepted_positions == 1
    assert result.ending_equity == 950
    assert result.kill_switch_blocked_entries == 1


@pytest.mark.parametrize('future_close', [50, 100, 150])
def test_opening_gap_blocks_even_if_that_minutes_future_close_recovers(future_close):
    result = run([held(), attempt('2026-01-06 14:30')], minutes(
        ['2026-01-05 20:59', '2026-01-06 14:30'], [100, 90], [100, future_close]))
    assert result.accepted_positions == 1
    assert result.kill_switch_blocked_entries == 1


def test_future_loss_cannot_block_an_earlier_opening_entry():
    result = run([held(), attempt('2026-01-06 14:30')], minutes(
        ['2026-01-05 20:59', '2026-01-06 14:30'], [100, 100], [100, 50]))
    assert result.accepted_positions == 2
    assert result.kill_switch_blocked_entries == 0


def test_recovery_reenables_entries_without_latching_the_guard():
    result = run([held(), attempt('2026-01-06 14:30', 'BLOCKED'),
                  attempt('2026-01-06 16:00', 'RECOVERED')], minutes(
        ['2026-01-05 20:59', '2026-01-06 14:30', '2026-01-06 16:00'],
        [100, 90, 100]))
    assert {t.ticker for t in result.trades} == {'HELD', 'RECOVERED'}
    assert result.kill_switch_blocked_entries == result.kill_switch_trip_days == 1


def test_sessions_without_candidates_still_use_the_last_session_close():
    # Day 1 closes 100, day 2 closes 90, day 3 closes 80. A day 4 sale at 75
    # loses 25/900 = 2.78%, so entry is allowed (not 125/1000 = 12.5%).
    result = run([held('2026-01-08 14:31', 75), attempt('2026-01-08 15:00')],
                 minutes(['2026-01-05 20:59', '2026-01-06 20:59',
                          '2026-01-07 20:59', '2026-01-08 14:30'], [100, 90, 80, 75]))
    assert result.accepted_positions == 2
    assert result.ending_equity == 875


def test_loss_after_several_candidate_free_sessions_is_not_erased():
    result = run([held('2026-01-08 14:31', 70), attempt('2026-01-08 15:00')],
                 minutes(['2026-01-05 20:59', '2026-01-06 20:59',
                          '2026-01-07 20:59', '2026-01-08 14:30'], [100, 90, 80, 70]))
    assert result.accepted_positions == 1  # 50/900 > 3%
    assert result.ending_equity == 850


def test_prior_session_exit_at_close_is_included_in_baseline():
    result = run([held('2026-01-06 21:00', 80), attempt('2026-01-08 15:00')],
                 minutes(['2026-01-05 20:59', '2026-01-06 20:59'], [100, 100]))
    assert result.accepted_positions == 2
    assert result.kill_switch_blocked_entries == 0


def test_early_close_holiday_and_after_hours_use_exchange_session_baseline():
    first = _single_candidate('HELD', '2025-11-26 15:00', 100, '2025-12-02 20:00', 100)
    result = run([first, attempt('2025-12-01 14:30')], minutes(
        ['2025-11-26 20:59', '2025-11-27 20:59', '2025-11-28 17:59',
         '2025-11-28 18:00', '2025-12-01 14:30'], [100, 200, 90, 200, 90]))
    assert result.accepted_positions == 2  # unchanged since Friday's 13:00 ET close
    assert result.kill_switch_blocked_entries == 0


@pytest.mark.parametrize('timeframe', ['1d', '4h'])
@pytest.mark.parametrize('future_close', [50, 150])
def test_signal_candle_future_close_cannot_change_entry_decision(monkeypatch, timeframe, future_close):
    from market_cache import MarketDataCache
    signal = minutes(['2026-01-05 05:00', '2026-01-06 05:00'], [100, 100], [100, future_close])
    signal.attrs.update(timeframe=timeframe, feed='sip', adjustment='all')
    execution = minutes(['2026-01-05 20:59', '2026-01-06 14:30'], [100, 90])
    requests = []

    def get_bars(self, ticker, start, end, requested_timeframe, **kwargs):
        requests.append((ticker, requested_timeframe, kwargs))
        return execution

    monkeypatch.setattr(MarketDataCache, 'get_bars', get_bars)
    result = run([held(), attempt('2026-01-06 14:30')], signal)
    assert result.accepted_positions == 1
    assert requests == [('HELD', '1min', {'feed': 'sip', 'adjustment': 'all'})]


def test_missing_held_price_cannot_silently_value_shares_at_zero():
    with pytest.raises(ValueError, match='HELD'):
        run([held(), attempt('2026-01-06 14:30')],
            minutes(['2026-01-07 14:30'], [100]))


def test_same_timestamp_exit_loss_counts_without_releasing_cash_early():
    # A stop was hit during 14:30 and booked at 14:31; that minute then rebounds.
    # The loss is real even though exit cash remains unavailable to this entry.
    result = run([held('2026-01-06 14:31', 90), attempt('2026-01-06 14:31')],
                 minutes(['2026-01-05 20:59', '2026-01-06 14:30',
                          '2026-01-06 14:31'], [100, 100, 100]))
    assert result.accepted_positions == 1
    assert result.kill_switch_blocked_entries == 1


def test_minute_close_is_observable_only_at_minute_end():
    result = run([held(), attempt('2026-01-06 14:31')], minutes(
        ['2026-01-05 20:59', '2026-01-06 14:30'], [100, 100], [100, 90]))
    assert result.accepted_positions == 1
    assert result.kill_switch_blocked_entries == 1


def test_new_minute_open_takes_precedence_over_preceding_close():
    result = run([held(), attempt('2026-01-06 14:31')], minutes(
        ['2026-01-05 20:59', '2026-01-06 14:30', '2026-01-06 14:31'],
        [100, 100, 100], [100, 90, 50]))
    assert result.accepted_positions == 2


def test_dst_weekend_baseline_and_opening_loss():
    first = _single_candidate('HELD', '2026-03-06 14:30', 100, '2026-03-10 20:00', 100)
    result = run([first, attempt('2026-03-09 13:30')], minutes(
        ['2026-03-06 20:59', '2026-03-09 13:30'], [100, 90], [100, 150]))
    assert result.accepted_positions == 1


@pytest.mark.parametrize('metadata', [{}, {'timeframe': '4h'}])
def test_ambiguous_custom_bars_do_not_silently_use_future_closes(metadata):
    frame = minutes(['2026-01-05 15:00'], [100])
    frame.attrs = metadata
    with pytest.raises(ValueError):
        run([held(), attempt('2026-01-06 14:30')], frame)
