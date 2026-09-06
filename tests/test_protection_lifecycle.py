"""Protection contracts: real SDK requests and SQLite; simulated broker only."""
from types import SimpleNamespace as NS

import pandas as pd
import pytest

import bot
from dashboard import db


class NotFound(Exception):
    status_code = 404


def order(oid, coid, *, side="sell", qty=2, filled=0, status="new",
          kind="stop", tif="gtc", price=None):
    return NS(id=oid, client_order_id=coid, symbol="AMD", side=side,
              qty=str(qty), filled_qty=str(filled), filled_avg_price=price,
              status=status, type=kind, time_in_force=tif, legs=[],
              stop_price="90" if kind == "stop" else None,
              limit_price="110" if kind == "limit" else None)


class Broker:
    def __init__(self, strategy="ensemble", status="expired"):
        self.strategy = strategy
        self.stop = order("sl", "broker-sl", status=status)
        self.tp = order("tp", "broker-tp", kind="limit", status=status)
        self.entry = order("entry", f"swingv2-entry-{strategy}-AMD-test",
                           side="buy", filled=2, status="filled", kind="market", price="100")
        self.entry.legs = [self.stop] if strategy == "sma_50_cross" else [self.tp, self.stop]
        self.orders = {o.id: o for o in [self.entry, *self.entry.legs]}
        self.submitted, self.canceled = [], []
        self.price, self.position_qty, self.available = 100, 5, 5
        self.fail_submit = None
        self.lookup_unknown = False
        self.cancel_pending = False
        self.fill_on_cancel = False

    def get_order_by_id(self, oid, **kwargs):
        return self.orders[oid]

    def get_order_by_client_id(self, coid):
        if self.lookup_unknown:
            raise ConnectionError("unavailable")
        for item in self.orders.values():
            if item.client_order_id == coid:
                return item
        raise NotFound()

    def get_orders(self, **kwargs):
        return []

    def get_open_position(self, ticker):
        return NS(qty=str(self.position_qty), qty_available=str(self.available),
                  current_price=str(self.price))

    def cancel_order_by_id(self, oid):
        self.canceled.append(oid)
        target = self.orders[oid]
        target.status = "pending_cancel" if self.cancel_pending else "canceled"
        if self.fill_on_cancel and oid == "tp":
            target.filled_qty, target.filled_avg_price = "1", "110"

    def submit_order(self, request):
        # Persistence is a safety boundary: a timeout must not erase this ID.
        if request.type != "market":
            trade = db.get_open_trade("AMD", self.strategy)
            assert trade["protect_client_order_id"] == request.client_order_id
        self.submitted.append(request)
        if self.fail_submit == "unaccepted":
            raise TimeoutError("unknown outcome")
        root = order(f"repair-{len(self.submitted)}", request.client_order_id,
                     qty=request.qty, kind=request.type)
        if request.order_class == "oco":
            stop = order(root.id + "-sl", "broker-repair-sl", qty=request.qty)
            root.legs = [stop]
            self.orders[stop.id] = stop
        self.orders[root.id] = root
        if self.fail_submit == "accepted":
            raise TimeoutError("accepted but response lost")
        return root


@pytest.fixture
def lifecycle(monkeypatch):
    monkeypatch.setattr(bot, "send_notification", lambda *a, **k: None)
    monkeypatch.setattr(bot, "_days_held", lambda _: 0)
    monkeypatch.setattr(bot.time, "sleep", lambda _: None)
    monkeypatch.setattr(bot, "_signal_exit_frame", lambda *a: pd.DataFrame())

    def setup(strategy="ensemble", status="expired"):
        broker = Broker(strategy, status)
        trade_id = db.save_trade("AMD", strategy, "2026-09-01", 100, 90,
                                 0 if strategy == "sma_50_cross" else 110, shares=2,
                                 client_order_id=broker.entry.client_order_id,
                                 alpaca_order_id=broker.entry.id)
        monkeypatch.setattr(bot, "_get_trading", lambda: broker)
        def run():
            bot._reconcile_and_exit(strategy, {})
            return db.get_all_trades(limit=1)[0]
        return broker, trade_id, run
    return setup


@pytest.mark.parametrize("strategy", ["ensemble", "sma_50_cross"])
def test_entries_request_multi_session_protection(strategy):
    requests = []
    client = NS(submit_order=lambda req: requests.append(req) or NS(id="entry"))
    if strategy == "ensemble":
        bot._place_single_bracket_entry(client, "AMD", 2, NS(tp3=110, stop_loss=90), strategy)
    else:
        bot._place_stop_only_entry(client, "AMD", 2, 90, strategy)
    request = requests[0]
    assert request.time_in_force == "gtc"
    assert not request.extended_hours
    assert request.qty == 2 and request.notional is None


@pytest.mark.parametrize("strategy", ["ensemble", "sma_50_cross"])
def test_expired_protection_repaired_once_even_without_signal_data(lifecycle, strategy):
    broker, _, run = lifecycle(strategy)
    trade = run()
    assert len(broker.submitted) == 1
    request = broker.submitted[0]
    assert request.qty == 2  # three foreign shares are never protected/sold
    assert request.time_in_force == "gtc" and not request.extended_hours
    assert request.side == "sell"
    if strategy == "ensemble":
        assert request.type == "limit" and request.order_class == "oco"
        assert request.take_profit.limit_price == 110
        assert request.stop_loss.stop_price == 90
    else:
        assert request.type == "stop" and request.stop_price == 90
        assert request.take_profit is None
    assert trade["protect_alpaca_order_id"] == "repair-1"
    run()
    assert len(broker.submitted) == 1


def test_healthy_gtc_protection_is_left_alone(lifecycle):
    broker, _, run = lifecycle(status="new")
    run()
    assert broker.submitted == broker.canceled == []


@pytest.mark.parametrize("status", ["held", "done_for_day"])
def test_gtc_resting_session_states_do_not_cause_cancel_and_rearm(lifecycle, status):
    broker, _, run = lifecycle(status=status)
    run()
    assert broker.submitted == broker.canceled == []


@pytest.mark.parametrize("problem", ["missing_leg", "foreign_parent", "unsettled_entry"])
def test_missing_ownership_evidence_blocks_protection(lifecycle, problem):
    broker, _, run = lifecycle()
    if problem == "missing_leg":
        broker.entry.legs = [broker.stop]
    elif problem == "foreign_parent":
        broker.entry.client_order_id = "foreign-entry"
    else:
        broker.entry.status = "partially_filled"
    run()
    assert broker.submitted == broker.canceled == []


def test_persist_failure_prevents_protection_submission(lifecycle, monkeypatch):
    broker, _, run = lifecycle()
    def fail(*args):
        raise RuntimeError("disk full")
    monkeypatch.setattr(db, "set_protect_order_ids", fail)
    run()
    assert broker.submitted == []


def test_failed_broker_id_write_recovers_by_durable_client_id(lifecycle, monkeypatch):
    broker, _, run = lifecycle()
    original = db.set_protect_order_ids
    def fail_accepted(trade_id, coid, oid):
        if oid:
            raise RuntimeError("disk full")
        original(trade_id, coid, oid)
    monkeypatch.setattr(db, "set_protect_order_ids", fail_accepted)
    first = run()
    assert first["protect_client_order_id"] and first["protect_alpaca_order_id"] is None
    monkeypatch.setattr(db, "set_protect_order_ids", original)
    second = run()
    assert second["protect_alpaca_order_id"] == "repair-1"
    assert len(broker.submitted) == 1


def test_zero_fill_rejection_allows_later_repair(lifecycle):
    broker, _, run = lifecycle()
    submit = broker.submit_order
    class Rejected(Exception):
        status_code = 422
    def reject(request):
        raise Rejected("invalid stop against moving market")
    broker.submit_order = reject
    first = run()
    assert first["protect_client_order_id"] is None
    broker.submit_order = submit
    run()
    assert len(broker.submitted) == 1


def test_day_protection_migrates_to_gtc_after_confirmed_cancel(lifecycle):
    broker, _, run = lifecycle(status="new")
    broker.tp.time_in_force = broker.stop.time_in_force = "day"
    run()
    assert set(broker.canceled) == {"tp", "sl"}
    assert len(broker.submitted) == 1
    assert broker.submitted[0].time_in_force == "gtc"


def test_partial_fill_during_repair_cancel_reduces_protection(lifecycle):
    broker, trade_id, run = lifecycle(status="new")
    broker.stop.qty = "1"  # underprotected; replace group
    broker.fill_on_cancel = True
    run()
    assert [req.qty for req in broker.submitted] == [1]
    assert db.get_exit_fill_totals(trade_id) == (1, 110)


def test_unconfirmed_cancel_never_creates_replacement(lifecycle):
    broker, _, run = lifecycle(status="new")
    broker.stop.time_in_force = "day"
    broker.cancel_pending = True
    run()
    assert broker.canceled and broker.submitted == []


def test_group_cancellation_rejection_still_repairs_after_terminal_confirmation(lifecycle):
    from copy import deepcopy
    broker, _, run = lifecycle(status="new")
    broker.get_order_by_id = lambda oid, **kwargs: deepcopy(broker.orders[oid])
    broker.tp.time_in_force = broker.stop.time_in_force = "day"
    def cancel(oid):
        broker.canceled.append(oid)
        if broker.orders[oid].status == "canceled":
            raise RuntimeError("422: order is already canceled")
        # Alpaca cancels the other OCO leg when one member is canceled.
        broker.tp.status = broker.stop.status = "canceled"
    broker.cancel_order_by_id = cancel
    run()
    assert broker.canceled
    assert [req.qty for req in broker.submitted] == [2]


def test_done_for_day_can_be_canceled_to_repair_wrong_quantity(lifecycle):
    broker, _, run = lifecycle(status="done_for_day")
    broker.stop.qty = "1"
    run()
    assert set(broker.canceled) == {"tp", "sl"}
    assert [req.qty for req in broker.submitted] == [2]


def test_unknown_sibling_state_blocks_all_cancellations(lifecycle):
    broker, _, run = lifecycle(status="new")
    broker.stop.status = "pending_replace"
    run()
    assert broker.canceled == broker.submitted == []


def test_timeout_adopts_exact_repair_after_restart(lifecycle):
    broker, _, run = lifecycle()
    broker.fail_submit = "accepted"
    run()
    trade = run()
    assert len(broker.submitted) == 1
    assert trade["protect_alpaca_order_id"] == "repair-1"


def test_unknown_submit_blocks_new_repairs_and_manual_exit(lifecycle, monkeypatch):
    broker, _, run = lifecycle()
    broker.fail_submit = "unaccepted"
    trade = run()
    assert trade["protect_client_order_id"]
    broker.lookup_unknown = True
    monkeypatch.setattr(bot, "_days_held", lambda _: 10)
    run()
    assert len(broker.submitted) == 1


def test_second_expiration_preserves_prior_partial_fill_ledger(lifecycle):
    broker, trade_id, run = lifecycle()
    run()
    first = broker.orders["repair-1"]
    first.status, first.filled_qty, first.filled_avg_price = "expired", "1", "110"
    first.legs[0].status = "expired"
    run()
    run()
    assert [req.qty for req in broker.submitted] == [2, 1]
    assert db.get_exit_fill_totals(trade_id) == (1, 110)


@pytest.mark.parametrize("strategy", ["ensemble", "sma_50_cross"])
def test_filled_replacement_closes_only_owned_inventory(lifecycle, strategy):
    broker, _, run = lifecycle(strategy)
    run()
    root = broker.orders["repair-1"]
    root.status, root.filled_qty, root.filled_avg_price = "filled", "2", "90"
    for leg in root.legs:
        leg.status = "canceled"
    trade = run()
    assert trade["status"] == "closed"
    assert trade["pnl_dollars"] == -20
    assert len(broker.submitted) == 1


def test_sma_manual_exit_cancels_replacement_stop(lifecycle):
    broker, _, run = lifecycle("sma_50_cross")
    trade = run()
    bot._close_owned(broker, trade, broker.get_open_position("AMD"), "sma_cross_down")
    assert "repair-1" in broker.canceled
    assert [req.type for req in broker.submitted] == ["stop", "market"]


@pytest.mark.parametrize("price", [89, 90, 90.005, 110, 111])
def test_breached_levels_use_controlled_exit_instead_of_invalid_oco(lifecycle, price):
    broker, _, run = lifecycle()
    broker.price = price
    trade = run()
    assert [req.type for req in broker.submitted] == ["market"]
    assert broker.submitted[0].qty == 2
    assert trade["exit_intent_reason"] == "protection_breached"


@pytest.mark.parametrize("problem", ["short", "insufficient", "reserved", "fractional", "unknown"])
def test_unverified_or_unavailable_inventory_blocks_repair(lifecycle, problem):
    broker, _, run = lifecycle()
    if problem == "short":
        broker.position_qty = -5
    elif problem == "insufficient":
        broker.position_qty = 1
    elif problem == "reserved":
        broker.available = 1
    elif problem == "fractional":
        broker.entry.filled_qty = "1.5"
    else:
        broker.stop.status = "pending_replace"
    run()
    assert broker.submitted == []
