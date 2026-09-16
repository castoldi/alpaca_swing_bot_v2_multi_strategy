"""F12: real reconciliation and isolated SQLite, with a simulated broker."""
from types import SimpleNamespace as NS

import pandas as pd
import pytest

import bot
from dashboard import db
from tests.test_protection_lifecycle import Broker, order


@pytest.fixture
def scenario(monkeypatch):
    monkeypatch.setattr(bot, 'send_notification', lambda *a, **k: None)
    monkeypatch.setattr(bot.time, 'sleep', lambda _: None)
    monkeypatch.setattr(bot, '_time_stop_due', lambda _: False)
    monkeypatch.setattr(bot, '_signal_exit_frame', lambda *a: pd.DataFrame())

    def setup(strategy='ensemble', status='canceled', path='pending'):
        broker = Broker(strategy=strategy, status='canceled')
        broker.entry.qty = '5'
        broker.entry.filled_qty = '2'
        broker.entry.filled_avg_price = '101'
        broker.entry.filled_at = '2026-09-01T14:30:00Z'
        broker.entry.status = status
        broker.get_open_position = lambda _: None
        tid = db.save_trade('AMD', strategy, '2026-09-01', 100, 90, 110, shares=5,
                            client_order_id=broker.entry.client_order_id, alpaca_order_id='entry')
        if path == 'pending':
            sell = order('exit', f'swingv2-exit-{strategy}-AMD-test', qty=2,
                         filled=2, status='filled', kind='market', price='110')
            broker.orders['exit'] = sell
            db.set_exit_intent(tid, 'time_stop', sell.client_order_id)
            db.set_exit_pending(tid, sell.client_order_id, sell.id)
        elif path == 'protective':
            broker.stop.filled_qty = '2'
            broker.stop.filled_avg_price = '110'
            broker.stop.status = 'filled'
        monkeypatch.setattr(bot, '_get_trading', lambda: broker)

        def run():
            bot._reconcile_and_exit(strategy, {})
            return db.get_all_trades(limit=1)[0]
        return broker, tid, run
    return setup


@pytest.mark.parametrize('status', ['canceled', 'expired', 'rejected'])
@pytest.mark.parametrize('strategy', ['ensemble', 'sma_50_cross'])
@pytest.mark.parametrize('path', ['pending', 'protective'])
def test_terminal_partial_entry_closes_actual_quantity_and_basis(scenario, status, strategy, path):
    broker, tid, run = scenario(strategy, status, path)
    for _ in range(2):
        trade = run()
        assert trade['status'] == 'closed'
        assert trade['shares'] == 2
        assert trade['entry_filled_price'] == 101
        assert trade['pnl_dollars'] == 18
        assert trade['pnl_pct'] == pytest.approx(9 / 101)
        assert trade['requested_shares'] == 5
        assert db.get_exit_fill_totals(tid) == (2, 220)
        assert broker.submitted == broker.canceled == []


@pytest.mark.parametrize('problem', ['unavailable', 'id', 'symbol', 'client_id', 'side', 'unsettled', 'bad_price', 'zero_with_pending_exit'])
def test_unverified_entry_cannot_finalize_pending_exit(scenario, problem):
    broker, tid, run = scenario()
    if problem == 'unavailable':
        del broker.orders['entry']
    elif problem == 'unsettled':
        broker.entry.status = 'partially_filled'
    elif problem == 'bad_price':
        broker.entry.filled_avg_price = 'nan'
    elif problem == 'zero_with_pending_exit':
        broker.entry.filled_qty = '0'
    else:
        setattr(broker.entry, {'id':'id', 'symbol':'symbol', 'client_id':'client_order_id', 'side':'side'}[problem], 'other')
    trade = run()
    assert trade['status'] == 'open'
    assert trade['shares'] == 5
    assert trade['exit_alpaca_order_id'] == 'exit'
    assert db.get_exit_fill_totals(tid) == (0, 0)
    assert broker.submitted == broker.canceled == []


@pytest.mark.parametrize('status', ['canceled', 'expired'])
def test_immediate_poll_records_positive_terminal_partial_fill(scenario, status):
    broker, tid, _ = scenario(status=status)
    bot._record_entry_fill(broker, tid, 'entry', attempts=1)
    trade = db.get_all_trades(limit=1)[0]
    assert trade['shares'] == 2
    assert trade['entry_filled_price'] == 101
    assert trade['requested_shares'] == 5


def test_legacy_requested_quantity_is_not_invented():
    tid = db.save_trade('AMD', 'ensemble', '2026-09-01', 100, 90, 110, shares=2)
    with db._con() as connection:
        connection.execute('ALTER TABLE trades DROP COLUMN requested_shares')
    db.init_db()
    db.init_db()
    assert db.get_open_trade('AMD', 'ensemble')['requested_shares'] is None
    db.set_entry_fill(tid, 101, 2)
    assert db.get_open_trade('AMD', 'ensemble')['requested_shares'] is None


@pytest.mark.parametrize('strategy', ['ensemble', 'sma_50_cross'])
def test_remaining_partial_entry_gets_only_actual_quantity_of_protection(scenario, strategy):
    broker, tid, run = scenario(strategy=strategy, path='open')
    broker.get_open_position = lambda _: NS(qty='5', qty_available='5', current_price='100')
    for _ in range(2):
        trade = run()
        assert trade['shares'] == 2
        assert trade['requested_shares'] == 5
        assert trade['entry_filled_price'] == 101
        assert len(broker.submitted) == 1
        assert broker.submitted[0].qty == 2
        assert broker.submitted[0].type != 'market'


def test_partial_pending_exit_preserves_remainder_and_survives_replay(scenario):
    broker, tid, run = scenario()
    broker.orders['exit'].status = 'canceled'
    broker.orders['exit'].filled_qty = '1'
    broker.get_open_position = lambda _: NS(qty='4', qty_available='4', current_price='105')
    trade = run()
    assert trade['status'] == 'open'
    assert trade['shares'] == 2
    assert db.get_exit_fill_totals(tid) == (1, 110)
    run()
    run()
    assert len(broker.submitted) == 1
    assert broker.submitted[0].qty == 1
    assert broker.submitted[0].type == 'market'
    replacement = broker.orders['repair-1']
    replacement.status, replacement.filled_qty, replacement.filled_avg_price = 'filled', '1', '110'
    trade = run()
    assert trade['status'] == 'closed'
    assert trade['pnl_dollars'] == 18
    assert trade['requested_shares'] == 5
    assert db.get_exit_fill_totals(tid) == (2, 220)
