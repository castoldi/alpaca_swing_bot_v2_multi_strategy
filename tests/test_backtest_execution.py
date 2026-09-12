"""Execution-clock regressions with synthetic prices and a real exchange calendar."""
from dataclasses import replace

import pandas as pd
import pytest

from backtest_portfolio import collect_backtest_candidates
from config import PARAMS
from strategies.base import BaseStrategy, EntrySignal, simulate_exit, simulate_exit_scaleout


def bars(times, rows=None):
    rows = rows or [(100, 101, 99, 100)] * len(times)
    frame = pd.DataFrame(rows, columns=['open', 'high', 'low', 'close'],
                         index=pd.to_datetime(times))
    frame['volume'] = 1000
    return frame


class Signal(BaseStrategy):
    name = 'execution_test'
    timeframe = '4h'

    def check_entry(self, df, idx, p=PARAMS):
        if idx == 0:
            return EntrySignal(df.index[idx], 100, 90, 110, 10, 55, self.name)


def collect(signal_frame, minutes, strategy=None, end='2026-12-31'):
    return collect_backtest_candidates(
        signal_frame, 'TEST', signal_frame.index[0], pd.Timestamp(end),
        PARAMS, strategy or Signal(), execution_bars=minutes,
    )


@pytest.mark.parametrize('scaled', [False, True])
def test_stop_gap_fills_at_available_open(scaled):
    frame = bars(['2026-01-02', '2026-01-05'], [(100, 101, 99, 100), (80, 85, 75, 82)])
    sig = EntrySignal(frame.index[0], 100, 90, 110, 10, 55)
    if scaled:
        leg = simulate_exit_scaleout(frame, 0, sig)[0]
        assert (leg.exit_price, leg.reason) == (80, 'gap_stop')
    else:
        assert simulate_exit(frame, 0, sig)[1:3] == (80, 'gap_stop')


def test_overnight_extremes_cannot_trigger_bracket():
    signals = bars(['2025-11-26 16:00', '2025-11-26 20:00'])
    minutes = bars(['2025-11-26 20:00', '2025-11-26 21:00', '2025-11-28 14:30'],
                   [(100, 101, 99, 100), (80, 120, 70, 80), (100, 111, 99, 110)])
    candidate = collect(signals, minutes)[0]
    assert candidate.entry_date == pd.Timestamp('2025-11-26 20:00')
    assert candidate.single_legs[0].reason == 'take_profit'
    assert candidate.single_legs[0].exit_date == pd.Timestamp('2025-11-28 14:31')


def test_late_signal_waits_through_holiday_for_regular_open():
    signals = bars(['2025-11-26 20:00', '2025-11-27 00:00'])
    minutes = bars(['2025-11-27 00:00', '2025-11-27 15:00', '2025-11-28 14:29',
                    '2025-11-28 14:30'],
                   [(80, 81, 79, 80)] * 3 + [(101, 102, 100, 101)])
    candidate = collect(signals, minutes)[0]
    assert candidate.entry_date == pd.Timestamp('2025-11-28 14:30')
    assert candidate.entry_price == 101
    assert candidate.signal_available_at == pd.Timestamp('2025-11-27 00:00')


def test_early_close_excludes_after_hours_and_weekend():
    signals = bars(['2025-11-28 14:00', '2025-11-28 18:00'])
    minutes = bars(['2025-11-28 18:00', '2025-11-29 14:30', '2025-12-01 14:30'])
    assert collect(signals, minutes)[0].entry_date == pd.Timestamp('2025-12-01 14:30')


def test_dst_changes_regular_open_in_utc():
    from backtest_execution import regular_minutes
    minutes = bars(['2026-03-06 13:30', '2026-03-06 14:30',
                    '2026-03-09 13:29', '2026-03-09 13:30'])
    assert list(regular_minutes(minutes).index) == list(pd.to_datetime(
        ['2026-03-06 14:30', '2026-03-09 13:30']))


def test_ambiguous_minute_uses_stop_before_target():
    signals = bars(['2026-03-09 12:00', '2026-03-09 16:00'])
    minutes = bars(['2026-03-09 16:00'], [(100, 112, 88, 100)])
    leg = collect(signals, minutes)[0].single_legs[0]
    assert (leg.reason, leg.exit_price) == ('stop_loss', 90)
    assert leg.exit_date == pd.Timestamp('2026-03-09 16:01')


def test_opening_target_precedes_later_same_minute_stop():
    signals = bars(['2026-03-09 12:00', '2026-03-09 16:00'])
    minutes = bars(['2026-03-09 16:00', '2026-03-09 16:01'],
                   [(100, 101, 99, 100), (115, 116, 89, 100)])
    leg = collect(signals, minutes)[0].single_legs[0]
    assert (leg.reason, leg.exit_price) == ('take_profit', 115)
    assert leg.exit_date == pd.Timestamp('2026-03-09 16:01')


def test_no_eligible_execution_before_window_end_means_no_entry():
    signals = bars(['2025-11-28 14:00', '2025-11-28 18:00'])
    minutes = bars(['2025-11-28 18:00', '2025-12-01 14:30'])
    assert collect(signals, minutes, end='2025-11-28 23:59') == []


def test_gap_guard_uses_regular_fill_not_overnight_price():
    signals = bars(['2025-11-26 20:00', '2025-11-27 00:00'])
    minutes = bars(['2025-11-27 00:00', '2025-11-28 14:30'],
                   [(100, 101, 99, 100), (105, 106, 104, 105)])
    assert collect(signals, minutes) == []


def test_coarse_prices_require_explicit_execution_data():
    frame = bars(['2026-03-09 12:00', '2026-03-09 16:00'])
    with pytest.raises(ValueError, match='execution'):
        collect_backtest_candidates(frame, 'TEST', frame.index[0], frame.index[-1],
                                    PARAMS, Signal())


def test_empty_execution_data_fails_instead_of_using_coarse_bar():
    frame = bars(['2026-03-09 12:00', '2026-03-09 16:00'])
    with pytest.raises(ValueError, match='execution'):
        collect(frame, bars([]))


class DailySignal(Signal):
    timeframe = '1d'
    exit_mode = 'signal_with_stop'
    has_take_profit = False

    def check_exit(self, df, idx, p=PARAMS):
        return 'sma_cross_down' if idx == 1 else None


def test_daily_signal_exit_waits_for_next_session_after_completed_bar():
    # Alpaca daily labels are midnight New York, expressed in UTC.
    signals = bars(['2025-11-25 05:00', '2025-11-26 05:00', '2025-11-28 05:00'])
    minutes = bars(['2025-11-26 14:30', '2025-11-26 20:59', '2025-11-26 21:00',
                    '2025-11-28 14:30'])
    candidate = collect(signals, minutes, DailySignal())[0]
    assert candidate.entry_date == pd.Timestamp('2025-11-26 14:30')
    leg = candidate.single_legs[0]
    assert (leg.reason, leg.exit_date) == ('sma_cross_down', pd.Timestamp('2025-11-28 14:30'))


def test_production_metadata_loads_matching_cached_minute_feed(monkeypatch):
    from market_cache import MarketDataCache
    signal_frame = bars(['2026-03-09 12:00', '2026-03-09 16:00'])
    signal_frame.attrs.update(timeframe='4h', feed='sip', adjustment='all')
    minutes = bars(['2026-03-09 16:00'], [(101, 102, 100, 101)])

    def get_bars(self, ticker, start, end, timeframe, **options):
        assert (ticker, timeframe, options) == ('TEST', '1min', {'feed': 'sip', 'adjustment': 'all'})
        return minutes

    monkeypatch.setattr(MarketDataCache, 'get_bars', get_bars)
    candidates = collect_backtest_candidates(
        signal_frame, 'TEST', signal_frame.index[0], pd.Timestamp('2026-03-10'), PARAMS, Signal())
    assert candidates[0].entry_price == 101


def test_minute_downloader_uses_alpaca_minute_timeframe(monkeypatch):
    import data_feed
    from alpaca.data.timeframe import TimeFrameUnit
    from types import SimpleNamespace

    class Client:
        def get_stock_bars(self, request):
            assert request.timeframe.amount == 1
            assert request.timeframe.unit == TimeFrameUnit.Minute
            return SimpleNamespace(df=bars(['2026-03-09 16:00']))

    monkeypatch.setattr(data_feed, '_get_client', lambda: Client())
    out = data_feed.fetch_bars('TEST', pd.Timestamp('2026-03-09'),
                               pd.Timestamp('2026-03-10'), '1min', feed='sip', strict=True)
    assert out['open'].tolist() == [100]


def test_history_loader_marks_signal_feed_and_timeframe(monkeypatch):
    import backtest_2025
    from types import SimpleNamespace
    from datetime import date

    monkeypatch.setattr(backtest_2025, '_MARKET_CACHE', SimpleNamespace(
        get_bars=lambda *a, **k: bars(['2025-11-26 16:00'])))
    frame = backtest_2025.download_history('TEST', date(2025, 1, 1), date(2025, 12, 31), '4h')
    assert frame.attrs['feed'] == 'sip'
    assert frame.attrs['timeframe'] == '4h'
    assert frame.attrs['adjustment'] == 'all'


def test_scaled_remainder_gaps_below_its_raised_stop():
    signals = bars(['2026-03-09 12:00', '2026-03-09 16:00'])
    minutes = bars(['2026-03-09 16:00', '2026-03-09 16:01'],
                   [(100, 104, 99, 103), (98, 99, 97, 98)])
    legs = collect(signals, minutes)[0].scaled_legs
    assert [leg.reason for leg in legs] == ['tp1', 'gap_stop']
    assert legs[-1].exit_price == 98
    assert legs[-1].fraction == pytest.approx(0.67)


def test_time_stop_counts_signal_bars_and_queues_until_session_open():
    signals = bars(['2026-03-09 12:00', '2026-03-09 16:00', '2026-03-09 20:00'],
                   [(100, 102, 99, 101)] * 3)
    minutes = bars(['2026-03-09 16:00', '2026-03-09 16:01', '2026-03-09 16:02',
                    '2026-03-09 19:59', '2026-03-10 13:30'])
    candidate = collect_backtest_candidates(
        signals, 'TEST', signals.index[0], pd.Timestamp('2026-03-11'),
        replace(PARAMS, max_holding_days=2), Signal(), execution_bars=minutes,
    )[0]
    for leg in (candidate.single_legs[-1], candidate.scaled_legs[-1]):
        assert (leg.reason, leg.bars_held) == ('time_stop', 2)
        assert leg.exit_date == pd.Timestamp('2026-03-10 13:30')


def test_execution_bars_after_boundary_cannot_supply_a_terminal_close():
    signals = bars(['2026-03-09 12:00', '2026-03-09 16:00'])
    minutes = bars(['2026-03-09 16:00', '2026-03-09 16:01'],
                   [(100, 102, 99, 101), (100, 120, 50, 60)])
    leg = collect(signals, minutes, end='2026-03-09 16:01')[0].single_legs[0]
    assert leg.reason == 'end_of_data'
    assert (leg.exit_price, leg.exit_date) == (101, pd.Timestamp('2026-03-09 16:01'))


def test_minute_touch_proceeds_cannot_fund_same_minute_entry():
    from backtest_portfolio import run_annual_portfolio
    signals = bars(['2026-03-09 12:00', '2026-03-09 16:00'])
    first = replace(collect(signals, bars(['2026-03-09 16:00'], [(100, 111, 99, 110)]))[0], ticker='A')
    second = replace(first, ticker='B', single_legs=(
        replace(first.single_legs[0], exit_price=100),))
    result = run_annual_portfolio([first, second], initial_equity=100,
                                  position_fraction=1, max_positions=5, apply_tax=False)
    assert result.accepted_positions == 1
    assert result.ending_equity == 110


def test_history_and_execution_failure_never_fall_back_to_strategy_ohlc(monkeypatch):
    from market_cache import MarketDataCache
    signals = bars(['2026-03-09 12:00', '2026-03-09 16:00'])
    signals.attrs.update(timeframe='4h', feed='sip', adjustment='all')
    def unavailable(*a, **k):
        raise RuntimeError('minute history unavailable')
    monkeypatch.setattr(MarketDataCache, 'get_bars', unavailable)
    with pytest.raises(RuntimeError, match='minute history unavailable'):
        collect_backtest_candidates(signals, 'TEST', signals.index[0], signals.index[-1],
                                    PARAMS, Signal())


def test_ticker_wrapper_includes_execution_during_last_signal_candle():
    from strategies.base import backtest_ticker
    signals = bars(['2026-03-09 12:00', '2026-03-09 16:00'])
    minutes = bars(['2026-03-09 16:00', '2026-03-09 16:01'],
                   [(100, 101, 99, 100), (100, 111, 99, 110)])
    trades = backtest_ticker(signals, 'TEST', signals.index[0], PARAMS, Signal(),
                             execution_bars=minutes)
    assert len(trades) == 1
    assert trades[0].exit_price == 110


@pytest.mark.parametrize('metadata', [
    {'timeframe': '4h'}, {'timeframe': '1d'}, {'feed': 'iex'}, {'adjustment': 'raw'},
])
def test_explicit_execution_data_rejects_conflicting_provenance(metadata):
    signals = bars(['2026-03-09 12:00', '2026-03-09 16:00'])
    signals.attrs.update(timeframe='4h', feed='sip', adjustment='all')
    minutes = bars(['2026-03-09 16:00'])
    minutes.attrs.update(metadata)
    with pytest.raises(ValueError, match='execution'):
        collect(signals, minutes)


def test_exit_signal_received_before_delayed_entry_fill_is_preserved():
    class IntradaySignal(DailySignal):
        timeframe = '4h'

    # Entry becomes known at Monday's close. An opposite signal is known at
    # midnight, before the queued entry can fill at Tuesday's regular open.
    signals = bars(['2026-03-09 16:00', '2026-03-09 20:00'])
    minutes = bars(['2026-03-10 13:30', '2026-03-10 13:31'],
                   [(100, 101, 99, 100), (100, 102, 99, 101)])
    candidate = collect(signals, minutes, IntradaySignal())[0]
    leg = candidate.single_legs[0]
    assert candidate.entry_date == pd.Timestamp('2026-03-10 13:30')
    assert (leg.reason, leg.exit_date, leg.exit_price, leg.bars_held) == (
        'sma_cross_down', pd.Timestamp('2026-03-10 13:30'), 100, 0,
    )
