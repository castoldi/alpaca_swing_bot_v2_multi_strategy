from dataclasses import replace

import pandas as pd
import pytest

from config import PARAMS
from strategies.base import add_earnings_filter


def frame(times):
    return pd.DataFrame({'close': [100.0] * len(times)}, index=pd.to_datetime(times))


def record(store, observed, events, until):
    store.record('TEST', observed, events, valid_until=until, source='test archive')


def test_upcoming_event_counts_sessions_not_rows_and_includes_last_bar(tmp_path):
    from earnings_calendar import CalendarStore
    store = CalendarStore(tmp_path / 'events.db')
    record(store, '2026-09-01T00:00Z', ['2026-09-14T08:00-04:00'], '2026-09-15T00:00Z')
    data = frame(['2026-09-08 15:00', '2026-09-09 15:00', '2026-09-09 19:00',
                  '2026-09-10 15:00', '2026-09-11 19:00'])
    out = add_earnings_filter(data, 'TEST', PARAMS, store=store, decision_times=data.index)
    assert out.near_earnings.tolist() == [False, True, True, True, True]


@pytest.mark.parametrize('event, blocked', [
    ('2026-09-14T08:00-04:00', [True, False, False]),
    ('2026-09-14T16:30-04:00', [True, True, False]),
    ('2026-09-14', [True, True, False]),
])
def test_before_after_market_and_unknown_times(event, blocked, tmp_path):
    from earnings_calendar import CalendarStore
    store = CalendarStore(tmp_path / 'events.db')
    record(store, '2026-09-10T00:00Z', [event], '2026-09-16T00:00Z')
    data = frame(['2026-09-11 19:00', '2026-09-14 14:00', '2026-09-15 14:00'])
    out = add_earnings_filter(data, 'TEST', PARAMS, store=store, decision_times=data.index)
    assert out.near_earnings.tolist() == blocked


def test_holiday_is_not_counted_as_a_trading_session(tmp_path):
    from earnings_calendar import CalendarStore
    store = CalendarStore(tmp_path / 'events.db')
    record(store, '2026-09-01T00:00Z', ['2026-09-08T08:00-04:00'], '2026-09-09T00:00Z')
    data = frame(['2026-09-02 15:00', '2026-09-03 15:00', '2026-09-04 15:00'])
    out = add_earnings_filter(data, 'TEST', PARAMS, store=store, decision_times=data.index)
    assert out.near_earnings.tolist() == [True, True, True]


def test_historical_calendar_cannot_use_later_observations_or_revisions(tmp_path):
    from earnings_calendar import CalendarStore
    store = CalendarStore(tmp_path / 'events.db')
    record(store, '2026-09-10T15:00Z', ['2026-10-20T16:30-04:00'], '2026-09-12T00:00Z')
    record(store, '2026-09-11T15:00Z', ['2026-09-14T16:30-04:00'], '2026-09-12T00:00Z')
    data = frame(['2026-09-10 14:00', '2026-09-10 16:00', '2026-09-11 16:00'])
    out = add_earnings_filter(data, 'TEST', PARAMS, store=store, decision_times=data.index)
    assert out.earnings_status.tolist() == ['unknown', 'clear', 'blocked']
    assert out.near_earnings.tolist() == [True, False, True]


def test_refresh_failure_retries_and_does_not_stay_empty_forever(tmp_path):
    from earnings_calendar import CalendarStore
    calls = []
    def fetch(ticker):
        calls.append(ticker)
        if len(calls) == 1:
            raise RuntimeError('provider unavailable')
        return ['2026-10-20T16:30-04:00']
    store = CalendarStore(tmp_path / 'events.db', fetcher=fetch)
    data = frame(['2026-01-01'])  # Live must use current decision time, not this stale bar.
    for now, expected in [('2026-09-10T14:00Z', True), ('2026-09-10T14:01Z', True),
                          ('2026-09-10T14:06Z', False)]:
        out = add_earnings_filter(data, 'TEST', PARAMS, store=store, live=True, as_of=now)
        assert bool(out.near_earnings.iloc[-1]) is expected
    assert calls == ['TEST', 'TEST']


def test_expired_success_does_not_allow_entries_after_failed_refresh(tmp_path):
    from earnings_calendar import CalendarStore
    def fail(_):
        raise RuntimeError('provider unavailable')
    store = CalendarStore(tmp_path / 'events.db', fetcher=fail)
    record(store, '2026-09-10T00:00Z', ['2026-10-20T16:30-04:00'], '2026-09-10T06:00Z')
    out = add_earnings_filter(frame(['2026-09-01']), 'TEST', PARAMS, store=store,
                              live=True, as_of='2026-09-10T14:00Z')
    assert out.earnings_status.iloc[-1] == 'unknown'


def test_historical_filter_does_not_fetch_todays_calendar(tmp_path):
    from earnings_calendar import CalendarStore
    def forbidden(_):
        pytest.fail('Historical runs must not download a present-day earnings calendar')
    store = CalendarStore(tmp_path / 'events.db', fetcher=forbidden)
    out = add_earnings_filter(frame(['2025-01-02']), 'TEST', PARAMS, store=store)
    assert out.near_earnings.tolist() == [True]


def test_explicit_disable_does_not_require_earnings_data():
    out = add_earnings_filter(frame(['2025-01-02']), 'TEST', replace(PARAMS, earnings_avoid_days=0))
    assert out.near_earnings.tolist() == [False]


def test_live_upcoming_event_is_checked_at_now_despite_old_signal_bar(tmp_path):
    from earnings_calendar import CalendarStore
    store = CalendarStore(tmp_path / 'events.db', fetcher=lambda _: ['2026-09-14T16:30-04:00'])
    out = add_earnings_filter(frame(['2026-08-01']), 'TEST', PARAMS, store=store,
                              live=True, as_of='2026-09-11T15:00Z')
    assert out.earnings_status.tolist() == ['blocked']


def entry_frame():
    data = pd.DataFrame({
        'open': [98.0] * 61, 'high': [101.0] * 61, 'low': [97.0] * 61,
        'close': [99.0] * 60 + [100.0], 'volume': [1000] * 61,
        'sma_slow': [90.0] * 61, 'atr': [2.0] * 61,
        'rsi': [40.0] * 60 + [50.0],
    }, index=pd.date_range(end='2026-09-11 12:00', periods=61, freq='4h'))
    return data


def test_trend_pullback_requires_a_known_earnings_decision():
    from strategies.trend_pullback import TrendPullbackStrategy
    data = entry_frame()
    assert TrendPullbackStrategy().check_entry(data, 60, PARAMS) is None
    data['near_earnings'] = False
    assert TrendPullbackStrategy().check_entry(data, 60, PARAMS) is not None


@pytest.mark.parametrize('status', ['blocked', 'unknown', 'clear'])
def test_live_cycle_applies_earnings_before_generating_order(monkeypatch, tmp_path, status):
    import bot
    import earnings_calendar
    from config import StrategyType
    from strategies.trend_pullback import TrendPullbackStrategy
    from tests.test_bot_position_sizing import _configure_cycle, _AccountClient
    placed, _, reconciled = _configure_cycle(monkeypatch, _AccountClient(), ['TEST'], {'TEST': 100})
    monkeypatch.setitem(bot.REGISTRY, 'trend_pullback', TrendPullbackStrategy())
    monkeypatch.setattr(bot, 'fetch_bars', lambda *a, **k: entry_frame())
    now = pd.Timestamp.now(tz='America/New_York')
    events = [] if status == 'unknown' else [
        (now + pd.Timedelta(days=1 if status == 'blocked' else 60)).date().isoformat()]
    monkeypatch.setattr(earnings_calendar, '_STORE',
                        earnings_calendar.CalendarStore(tmp_path / 'live.db', fetcher=lambda _: events))
    bot.run_once(StrategyType.TREND_PULLBACK)
    assert len(placed) == (1 if status == 'clear' else 0)
    assert len(reconciled) == 1


def test_backtest_uses_snapshot_known_at_execution_not_signal_start(monkeypatch, tmp_path):
    import backtest_portfolio
    import earnings_calendar
    from strategies.trend_pullback import TrendPullbackStrategy
    data = entry_frame()
    store = earnings_calendar.CalendarStore(tmp_path / 'history.db')
    record(store, '2026-09-11T10:00Z', ['2026-10-20T16:30-04:00'], '2026-09-12T00:00Z')
    record(store, '2026-09-11T17:00Z', ['2026-09-14T08:00-04:00'], '2026-09-12T00:00Z')
    monkeypatch.setattr(earnings_calendar, '_STORE', store)
    monkeypatch.setattr(backtest_portfolio, 'add_indicators', lambda df, _: df.copy())
    minutes = pd.DataFrame({'open': [100], 'high': [105], 'low': [99], 'close': [104], 'volume': [1000]},
                           index=pd.to_datetime(['2026-09-11 19:00']))
    assert backtest_portfolio.collect_backtest_candidates(
        data, 'TEST', pd.Timestamp('2026-09-11 12:00'), pd.Timestamp('2026-09-12'),
        PARAMS, TrendPullbackStrategy(), execution_bars=minutes,
    ) == []


def test_ensemble_blocks_only_its_trend_vote():
    from types import SimpleNamespace
    from strategies.ensemble import EnsembleStrategy
    from strategies.trend_pullback import TrendPullbackStrategy
    strategy = EnsembleStrategy()
    strategy._members = {'trend_pullback': TrendPullbackStrategy(),
                         'regime': SimpleNamespace(check_entry=lambda *a: object())}
    data = entry_frame()
    data['near_earnings'] = True
    assert strategy.check_entry(data, 60, PARAMS) is None
    data['near_earnings'] = False
    assert strategy.check_entry(data, 60, PARAMS).strategy == 'ensemble_0.55'


def test_empty_provider_result_can_recover_on_a_later_refresh(tmp_path):
    from earnings_calendar import CalendarStore
    responses = iter([[], ['2026-10-20T16:30-04:00']])
    store = CalendarStore(tmp_path / 'empty.db', fetcher=lambda _: next(responses))
    data = frame(['2026-09-10'])
    before = add_earnings_filter(data, 'TEST', PARAMS, store=store, live=True, as_of='2026-09-10T14:00Z')
    after = add_earnings_filter(data, 'TEST', PARAMS, store=store, live=True, as_of='2026-09-10T14:06Z')
    assert before.earnings_status.tolist() == ['unknown']
    assert after.earnings_status.tolist() == ['clear']


def test_provider_parses_timezone_and_distinguishes_date_only(monkeypatch):
    import yfinance
    from types import SimpleNamespace
    from earnings_calendar import fetch_events
    dates = pd.DatetimeIndex(['2026-09-14 08:00', '2026-10-20 16:30', '2026-11-12 00:00'],
                             tz='America/New_York')
    data = pd.DataFrame({'Event Type': ['Earnings', 'Earnings', 'Earnings']}, index=dates)
    monkeypatch.setattr(yfinance, 'Ticker', lambda _: SimpleNamespace(get_earnings_dates=lambda limit: data))
    assert fetch_events('TEST') == ['2026-09-14T08:00:00-04:00', '2026-10-20T16:30:00-04:00', '2026-11-12']


def test_archive_persists_across_process_store_recreation(tmp_path):
    from earnings_calendar import CalendarStore
    path = tmp_path / 'persistent.db'
    record(CalendarStore(path), '2026-09-10T00:00Z', ['2026-09-14'], '2026-09-12T00:00Z')
    data = frame(['2026-09-11 15:00'])
    out = add_earnings_filter(data, 'TEST', PARAMS, store=CalendarStore(path), decision_times=data.index)
    assert out.earnings_status.tolist() == ['blocked']


def test_provider_refresh_reaches_source_instead_of_reusing_yfinance_http_cache(monkeypatch):
    from types import SimpleNamespace
    from yfinance.data import YfData
    from earnings_calendar import fetch_events
    shared = YfData()
    responses = iter(['September 14, 2026 at 8 AM EDT', 'October 20, 2026 at 4 PM EDT'])
    def get(*args, **kwargs):
        date = next(responses)
        return SimpleNamespace(text=f'''<table><tr><th>Symbol</th><th>Company</th>
          <th>Earnings Date</th><th>EPS Estimate</th><th>Reported EPS</th><th>Surprise (%)</th></tr>
          <tr><td>TEST</td><td>Test</td><td>{date}</td><td>1</td><td>-</td><td>-</td></tr></table>''')
    shared.cache_get.cache_clear()
    monkeypatch.setattr(shared, 'get', get)
    try:
        assert fetch_events('TEST') == ['2026-09-14T08:00:00-04:00']
        assert fetch_events('TEST') == ['2026-10-20T16:00:00-04:00']
    finally:
        shared.cache_get.cache_clear()


@pytest.mark.parametrize('event, expected', [('2026-10-20', 1), ('2026-09-14', 0)])
def test_dashboard_examples_use_historical_earnings_policy(monkeypatch, tmp_path, event, expected):
    import earnings_calendar
    from dashboard import strategy_examples
    from strategies.trend_pullback import TrendPullbackStrategy
    store = earnings_calendar.CalendarStore(tmp_path / 'examples.db')
    record(store, '2026-09-11T10:00Z', [event], '2026-09-12T00:00Z')
    monkeypatch.setattr(earnings_calendar, '_STORE', store)
    examples = strategy_examples._examples_for_strategy(TrendPullbackStrategy(), {'TEST': entry_frame()})
    assert len(examples) == expected


def test_dashboard_does_not_reuse_examples_generated_without_earnings_policy(monkeypatch, tmp_path):
    import json
    from dashboard import strategy_examples
    data = {'examples': {s.name: [] for s in strategy_examples.get_all()}, 'timeframes': {'trend_pullback': '4h'}}
    path = tmp_path / 'old_examples.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    monkeypatch.setattr(strategy_examples, '_CACHE_FILE', path)
    assert strategy_examples._load_disk_cache() is None
