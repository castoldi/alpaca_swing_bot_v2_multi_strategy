"""F13: entry failures must not starve real exit reconciliation (offline)."""
from types import SimpleNamespace as NS

import pandas as pd
import pytest

import bot
from config import StrategyType
from dashboard import db
from tests.test_protection_lifecycle import Broker, NotFound, order
from tests.test_bot_position_sizing import _SignalStrategy


def fail(*args, **kwargs):
    raise RuntimeError('injected failure')


@pytest.fixture
def cycle(monkeypatch):
    notifications, scanned = [], []
    monkeypatch.setattr(bot, 'send_notification', lambda *args: notifications.append(args))
    monkeypatch.setattr(bot.time, 'sleep', lambda _: None)
    monkeypatch.setattr(bot, '_refresh_tax_records', lambda: None)
    monkeypatch.setattr(bot, '_record_balance_snapshot', lambda *args: None)
    monkeypatch.setattr(bot, '_load_live_sizing', lambda _: None)
    monkeypatch.setattr(bot, 'strategy_universe', lambda *args: ['BAD', 'GOOD'])
    monkeypatch.setattr(bot, '_time_stop_due', lambda trade: True)

    def fetch(ticker, **kwargs):
        scanned.append(ticker)
        if ticker == 'AMD':
            raise RuntimeError('exit data unavailable')
        frame = pd.DataFrame(dict(open=100., high=102., low=98., close=100., volume=1000),
                             index=pd.date_range('2026-01-01', periods=65, freq='D'))
        frame.attrs['ticker'] = ticker
        return frame

    monkeypatch.setattr(bot, 'fetch_bars', fetch)
    monkeypatch.setitem(bot.REGISTRY, 'ensemble', NS(
        timeframe='4h', has_take_profit=True, exit_mode='bracket',
        check_entry=lambda *args: None))

    def setup(path='pending'):
        strategy = 'sma_50_cross' if path == 'signal_no_data' else 'ensemble'
        broker = Broker(strategy, status='expired')
        broker.entry.filled_at = '2026-09-01T14:30:00Z'
        broker.price = 105
        tid = db.save_trade('AMD', strategy, '2026-09-01', 100, 90, 110, shares=2,
                            client_order_id=broker.entry.client_order_id, alpaca_order_id='entry')
        if path == 'pending':
            sell = order('exit', f'swingv2-exit-{strategy}-AMD-test', qty=2,
                         filled=2, status='filled', kind='market', price='110')
            broker.orders['exit'] = sell
            db.set_exit_intent(tid, 'time_stop', sell.client_order_id)
            db.set_exit_pending(tid, sell.client_order_id, sell.id)
        elif path == 'protection':
            broker.price = 95  # time threshold reached, but below breakeven
        monkeypatch.setattr(bot, '_get_trading', lambda: broker)
        return broker, tid

    return NS(setup=setup, fetch=fetch, scanned=scanned, notifications=notifications)


@pytest.mark.parametrize('stage', ['fetch', 'completed', 'indicators', 'signal'])
@pytest.mark.parametrize('path', ['pending', 'time_stop', 'protection', 'signal_no_data'])
def test_bad_ticker_does_not_starve_holdings_or_later_tickers(cycle, monkeypatch, stage, path):
    broker, tid = cycle.setup(path)
    if stage == 'fetch':
        def fetch(ticker, **kwargs):
            if ticker == 'BAD':
                fail()
            return cycle.fetch(ticker, **kwargs)
        monkeypatch.setattr(bot, 'fetch_bars', fetch)
    else:
        owner, attr = {
            'completed': (bot.data_feed, 'completed_bars'),
            'indicators': (bot, 'add_indicators'),
            'signal': (bot.REGISTRY['ensemble'], 'check_entry'),
        }[stage]
        original = getattr(owner, attr)
        def apply(frame, *args, **kwargs):
            if frame.attrs['ticker'] == 'BAD':
                fail()
            return original(frame, *args, **kwargs)
        monkeypatch.setattr(owner, attr, apply)

    for _ in range(2):
        assert bot.run_once(StrategyType.ENSEMBLE) == 1
        trade = db.get_all_trades(limit=1)[0]
        if path == 'pending':
            assert trade['status'] == 'closed'
            assert trade['pnl_dollars'] == 20
            assert db.get_exit_fill_totals(tid) == (2, 220)
            assert broker.submitted == []
        else:
            assert len(broker.submitted) == 1
            assert broker.submitted[0].qty == 2  # never the three unrelated shares
            assert broker.submitted[0].side == 'sell'
            if path == 'time_stop':
                assert broker.submitted[0].type == 'market'
                assert trade['exit_intent_reason'] == 'time_stop'
                assert trade['exit_alpaca_order_id'] == 'repair-1'
            else:
                assert broker.submitted[0].type == ('stop' if path == 'signal_no_data' else 'limit')
                assert trade['protect_alpaca_order_id'] == 'repair-1'
        assert 'GOOD' in cycle.scanned
        run = db.get_recent_runs(1)[0]
        assert run['status'] == 'error' and run['finished_at']
        assert 'BAD' in run['error'] and 'injected failure' in run['error']
        assert run['orders_placed'] == 0


@pytest.mark.parametrize('stage', ['client', 'capacity', 'sizing', 'universe'])
def test_entry_setup_failure_still_reconciles_filled_exit(cycle, monkeypatch, stage):
    broker, tid = cycle.setup()
    if stage == 'client':
        clients = iter([None, broker])
        def get_client():
            client = next(clients)
            if client is None:
                fail()
            return client
        monkeypatch.setattr(bot, '_get_trading', get_client)
    else:
        monkeypatch.setattr(bot, {'capacity': '_entry_capacity_is_unsettled',
                                 'sizing': '_load_live_sizing', 'universe': 'strategy_universe'}[stage], fail)
    assert bot.run_once(StrategyType.ENSEMBLE) == 1
    assert db.get_all_trades(limit=1)[0]['status'] == 'closed'
    assert db.get_exit_fill_totals(tid) == (2, 220)
    assert broker.submitted == []


def test_error_notification_failure_cannot_skip_reconciliation_or_run_record(cycle, monkeypatch):
    broker, _ = cycle.setup()
    monkeypatch.setattr(bot, 'fetch_bars', fail)
    monkeypatch.setattr(bot, 'send_notification', fail)
    assert bot.run_once(StrategyType.ENSEMBLE) == 1
    assert db.get_all_trades(limit=1)[0]['status'] == 'closed'
    run = db.get_recent_runs(1)[0]
    assert run['status'] == 'error' and run['finished_at']
    assert broker.submitted == []


def test_successful_cycle_reconciles_and_records_success(cycle):
    broker, _ = cycle.setup()
    assert bot.run_once(StrategyType.ENSEMBLE) == 0
    assert db.get_all_trades(limit=1)[0]['status'] == 'closed'
    assert db.get_recent_runs(1)[0]['status'] == 'done'
    assert broker.submitted == []
    assert not any(title == 'Bot V2 Error' for title, *_ in cycle.notifications)


@pytest.mark.parametrize('failure', ['data', 'submission_recovery'])
def test_later_entries_respect_capacity_after_a_ticker_failure(cycle, monkeypatch, failure):
    broker, _ = cycle.setup()
    old_position = broker.get_open_position
    def position(ticker):
        if ticker != 'AMD':
            raise NotFound()
        return old_position(ticker)
    monkeypatch.setattr(broker, 'get_open_position', position)
    monkeypatch.setitem(bot.REGISTRY, 'ensemble', _SignalStrategy())
    monkeypatch.setattr(bot, '_load_live_sizing', lambda _: NS(
        equity=1000, remaining_cash=1000, remaining_slots=4, leveraged_notional=0,
        open_notional_by_ticker={}))
    monkeypatch.setattr(bot, '_tax_entry_block', lambda _: None)
    monkeypatch.setattr(bot.data_feed, 'fetch_snapshots', lambda symbols: {symbols[0]: {'price': 100}})

    def submit(request):
        broker.submitted.append(request)
        accepted = order('new-entry', request.client_order_id, side='buy', kind='market')
        accepted.symbol = request.symbol
        broker.orders[accepted.id] = accepted
        if failure == 'submission_recovery':
            raise TimeoutError('accepted but response lost')
        return accepted
    monkeypatch.setattr(broker, 'submit_order', submit)
    if failure == 'data':
        def fetch(ticker, **kwargs):
            if ticker == 'BAD':
                fail()
            return cycle.fetch(ticker, **kwargs)
        monkeypatch.setattr(bot, 'fetch_bars', fetch)
    else:
        # Simulate an unexpected error escaping the submission recovery handler.
        monkeypatch.setattr(bot, '_lookup_entry_by_client_id', fail)

    assert bot.run_once(StrategyType.ENSEMBLE) == 1
    assert len(broker.submitted) == 1
    request = broker.submitted[0]
    assert request.symbol == ('GOOD' if failure == 'data' else 'BAD')
    assert request.side == 'buy' and request.qty == 2
    trades = {t['ticker']: t for t in db.get_all_trades()}
    assert trades['AMD']['status'] == 'closed'  # recovery failure cannot starve another trade
    assert trades[request.symbol]['status'] == 'open'
    assert trades[request.symbol]['client_order_id'] == request.client_order_id
    assert trades[request.symbol]['entry_state'] == ('accepted' if failure == 'data' else 'pending_submission')
    assert 'GOOD' in cycle.scanned


def test_failed_signal_frame_is_discarded_before_exit_checks(cycle, monkeypatch):
    broker, _ = cycle.setup('signal_no_data')
    monkeypatch.setattr(bot, 'strategy_universe', lambda *args: ['AMD'])
    fetched = []
    def fetch(ticker, **kwargs):
        fetched.append(ticker)
        if len(fetched) > 1:
            raise RuntimeError('no fresh exit data')
        return cycle.fetch('BAD', **kwargs)
    def broken_signal(frame, *args):
        # A strategy may corrupt its working frame before raising.
        frame.loc[frame.index[-2], 'close'] = 101
        frame.loc[frame.index[-1], 'close'] = 0
        fail()
    monkeypatch.setattr(bot, 'fetch_bars', fetch)
    monkeypatch.setattr(bot.REGISTRY['sma_50_cross'], 'check_entry', broken_signal)
    assert bot.run_once(StrategyType.SMA_50_CROSS) == 1
    assert fetched == ['AMD', 'AMD']
    assert len(broker.submitted) == 1
    assert broker.submitted[0].type == 'stop'  # no sell based on the corrupt frame
    assert broker.submitted[0].qty == 2
    assert db.get_open_trade('AMD', 'sma_50_cross')['exit_intent_reason'] is None


def test_reconciliation_exception_preserves_entry_error_and_finishes_run(cycle, monkeypatch):
    cycle.setup()
    monkeypatch.setattr(bot, 'fetch_bars', fail)
    def broken_reconcile(*args):
        raise RuntimeError('reconciliation failed')
    monkeypatch.setattr(bot, '_reconcile_and_exit', broken_reconcile)
    assert bot.run_once(StrategyType.ENSEMBLE) == 1
    run = db.get_recent_runs(1)[0]
    assert run['status'] == 'error' and run['finished_at']
    assert 'injected failure' in run['error'] and 'reconciliation failed' in run['error']
    assert len(cycle.notifications) == 1
