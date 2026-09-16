from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

import config
import data_feed


@pytest.mark.parametrize('feed', ['iex', 'sip'])
@pytest.mark.parametrize('timeframe', ['4h', '1d'])
def test_live_history_snapshots_and_cache_share_config(monkeypatch, tmp_path, feed, timeframe):
    import backtest_2025
    from market_cache import MarketDataCache

    monkeypatch.setattr(config, 'MARKET_DATA_FEED', feed)
    raw = pd.DataFrame(dict(open=[100.], high=[101.], low=[99.], close=[100.], volume=[1000]),
                       index=pd.to_datetime(['2025-01-02']))
    requests = []

    class Client:
        def get_stock_bars(self, request):
            requests.append(request)
            return SimpleNamespace(df=raw)

        def get_stock_snapshot(self, request):
            requests.append(request)
            return {'AMD': SimpleNamespace(latest_trade=SimpleNamespace(
                price=100., timestamp=pd.Timestamp('2025-01-02', tz='UTC')))}

    monkeypatch.setattr(data_feed, '_get_client', lambda: Client())
    cache = MarketDataCache(tmp_path / 'cache.db')
    monkeypatch.setattr(backtest_2025, '_MARKET_CACHE', cache)
    live = data_feed.fetch_recent('AMD', timeframe=timeframe)
    historical = backtest_2025.download_history('AMD', date(2025, 1, 1), date(2025, 1, 3), timeframe)
    cached = cache.get_bars('AMD', date(2025, 1, 1), date(2025, 1, 3), timeframe)
    snapshot = data_feed.fetch_snapshots(['AMD'])['AMD']
    assert requests and all(request.feed.value == feed for request in requests)
    assert all(frame.attrs['feed'] == feed for frame in (live, historical, cached))
    assert snapshot['feed'] == feed
    assert historical.attrs['session_policy'] == config.MARKET_DATA_SESSION_POLICY
    assert cached.attrs['cache_snapshot']['feed'] == feed


@pytest.mark.parametrize('feed', ['iex', 'sip'])
def test_report_records_configured_policy(monkeypatch, feed):
    from build_report_2025 import build_report_2025
    monkeypatch.setattr(config, 'MARKET_DATA_FEED', feed)
    html = build_report_2025({}, {}, None)
    assert f'Alpaca {feed.upper()} historical data' in html
    assert 'adjustment=all' in html
    assert config.MARKET_DATA_SESSION_POLICY in html


def test_provider_failure_never_falls_back_to_another_feed(monkeypatch):
    monkeypatch.setattr(config, 'MARKET_DATA_FEED', 'sip')
    requested = []
    class Client:
        def get_stock_bars(self, request):
            requested.append(request.feed.value)
            raise RuntimeError('subscription denied')
        get_stock_snapshot = get_stock_bars
    monkeypatch.setattr(data_feed, '_get_client', lambda: Client())
    assert data_feed.fetch_recent('AMD').empty
    assert data_feed.fetch_snapshots(['AMD']) == {}
    assert requested == ['sip', 'sip']


def test_invalid_feed_is_rejected_before_request(monkeypatch):
    monkeypatch.setattr(config, 'MARKET_DATA_FEED', 'typo')
    with pytest.raises(ValueError, match='Unsupported stock feed'):
        data_feed.fetch_recent('AMD')
    with pytest.raises(ValueError, match='Unsupported stock feed'):
        data_feed.fetch_snapshots(['AMD'])


def test_report_preserves_input_feed_after_config_change(monkeypatch):
    from build_report_2025 import build_report_2025
    monkeypatch.setattr(config, 'MARKET_DATA_FEED', 'iex')
    frame = pd.DataFrame()
    frame.attrs.update(data_feed.market_data_policy('sip'))
    html = build_report_2025({}, {'breakout': {'AMD': (frame, [])}}, None)
    assert 'Alpaca SIP historical data' in html
    assert 'Alpaca IEX historical data' not in html


@pytest.mark.parametrize('feed', ['iex', 'sip'])
def test_new_database_runs_retain_the_source_at_start(monkeypatch, feed):
    from dashboard import db
    monkeypatch.setattr(config, 'MARKET_DATA_FEED', feed)
    run_id = db.start_backtest_run(2026, 'breakout', '4h')
    monkeypatch.setattr(config, 'MARKET_DATA_FEED', 'sip' if feed == 'iex' else 'iex')
    db.finish_backtest_run(run_id, 0, 0., 0., 0., 0., 0.)
    row = db.get_backtest_results()[0]
    assert f'Alpaca {feed.upper()}' in row['data_source']
    assert 'adjustment=all' in row['data_source']


def test_source_migration_preserves_unknown_legacy_rows():
    from dashboard import db
    with db._con() as connection:
        connection.execute('ALTER TABLE backtest_runs DROP COLUMN data_source')
        connection.execute("INSERT INTO backtest_runs (year,strategy,started_at) VALUES (2025,'breakout','old')")
        connection.execute('ALTER TABLE equity_curves DROP COLUMN data_source')
        connection.execute("INSERT INTO equity_curves (strategy,year,ts,equity,year_factor) VALUES ('breakout',2025,'2025-01-01',1000,1)")
    db.init_db()
    db.init_db()
    assert db.get_backtest_history()[0]['data_source'] is None
    assert db.get_equity_curves()['breakout'][0]['data_source'] is None


def test_equity_curve_retains_explicit_mixed_source():
    from dashboard import db
    source = 'yfinance before 2016 + Alpaca IEX (mixed research history)'
    db.save_equity_curve('sma_50_cross', 2016, [('2016-01-01', 1000.)], 1000., data_source=source)
    assert db.get_equity_curves()['sma_50_cross'][0]['data_source'] == source
