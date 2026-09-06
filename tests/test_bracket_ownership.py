"""Real exit lifecycle and SQLite ledger, with only the broker/notifications faked."""
from types import SimpleNamespace

import pytest

import bot
from dashboard import db


def order(oid, coid, side, qty=0, price=None, status="new", **extra):
    return SimpleNamespace(
        id=oid, client_order_id=coid, symbol="AMD", side=side,
        filled_qty=str(qty), filled_avg_price=price, status=status,
        legs=[], **extra,
    )


class Broker:
    def __init__(self, entry_qty=2, tp_qty=2, tp_status="filled"):
        self.tp = order("tp-1", "broker-tp", "sell", tp_qty, 110, tp_status)
        self.sl = order("sl-1", "broker-sl", "sell", status="canceled")
        self.entry = order("entry-1", "swingv2-entry-ensemble-AMD-own", "buy",
                           entry_qty, 100, "filled")
        self.entry.legs = [self.tp, self.sl]
        self.orders = {o.id: o for o in [self.entry, self.tp, self.sl]}
        self.submitted = []
        self.canceled = []
        self.unreadable = False
        self.cancel_mode = "normal"

    def get_order_by_id(self, oid, **kwargs):
        if self.unreadable:
            raise RuntimeError("broker unavailable")
        return self.orders[oid]

    def get_order_by_client_id(self, coid):
        return next(o for o in self.orders.values() if o.client_order_id == coid)

    def get_open_position(self, ticker):
        # Always includes three shares belonging to somebody else.
        remaining = float(self.entry.filled_qty) - float(self.tp.filled_qty)
        return SimpleNamespace(qty=str(3 + remaining), current_price="105")

    def get_orders(self, **kwargs):
        return []

    def cancel_order_by_id(self, oid):
        self.canceled.append(oid)
        target = self.orders[oid]
        if self.cancel_mode == "error":
            raise RuntimeError("cancel rejected")
        if self.cancel_mode == "read_error":
            self.unreadable = True
        if self.cancel_mode == "partial":
            target.filled_qty = "1"
            target.status = "canceled"
        elif self.cancel_mode == "pending":
            target.status = "pending_cancel"
        elif self.cancel_mode == "fill":
            target.status = "filled"
            target.filled_qty = self.entry.filled_qty
        else:
            target.status = "canceled"

    def submit_order(self, request):
        self.submitted.append(request)
        result = order("exit-1", request.client_order_id, "sell")
        self.orders[result.id] = result
        return result


@pytest.fixture
def lifecycle(monkeypatch):
    monkeypatch.setattr(bot, "send_notification", lambda *a, **k: None)
    monkeypatch.setattr(bot, "_days_held", lambda _: 10)
    monkeypatch.setattr(bot.time, "sleep", lambda _: None)
    trade_id = db.save_trade(
        "AMD", "ensemble", "2026-09-01", 100, 90, 110, shares=2,
        client_order_id="swingv2-entry-ensemble-AMD-own", alpaca_order_id="entry-1",
    )
    def run(broker):
        monkeypatch.setattr(bot, "_get_trading", lambda: broker)
        bot._reconcile_and_exit("ensemble", {})
        return db.get_all_trades(limit=1)[0]
    return trade_id, run


def test_filled_tp_closes_our_ledger_without_selling_foreign_shares(lifecycle):
    trade_id, run = lifecycle
    broker = Broker()
    trade = run(broker)
    assert broker.submitted == []
    assert broker.canceled == []
    assert trade["status"] == "closed"
    assert trade["pnl_dollars"] == pytest.approx(20)
    assert db.get_exit_fill_totals(trade_id) == (2, 220)
    run(broker)
    assert broker.submitted == []
    assert db.get_exit_fill_totals(trade_id) == (2, 220)


def test_partial_tp_sells_only_remaining_owned_share_and_survives_replay(lifecycle):
    trade_id, run = lifecycle
    broker = Broker(tp_qty=1, tp_status="partially_filled")
    trade = run(broker)
    assert [o.qty for o in broker.submitted] == [1]
    assert broker.canceled == ["tp-1"]
    assert db.get_exit_fill_totals(trade_id) == (1, 110)
    assert trade["status"] == "open"
    run(broker)
    assert len(broker.submitted) == 1
    assert db.get_exit_fill_totals(trade_id) == (1, 110)


@pytest.mark.parametrize("failure", [
    "unreadable", "wrong_client_id", "wrong_parent_id", "wrong_symbol", "wrong_side",
    "missing_legs", "missing_sibling", "cancel_pending", "cancel_error", "confirm_error",
])
def test_unverified_bracket_state_blocks_selling(lifecycle, failure):
    _, run = lifecycle
    broker = Broker(tp_qty=0, tp_status="new")
    if failure == "unreadable":
        broker.unreadable = True
    elif failure == "wrong_client_id":
        broker.entry.client_order_id = "another-bot-entry"
    elif failure == "wrong_parent_id":
        broker.entry.id = "other-entry"
    elif failure == "wrong_symbol":
        broker.entry.symbol = "NVDA"
    elif failure == "wrong_side":
        broker.entry.side = "sell"
    elif failure == "missing_legs":
        broker.entry.legs = []
    elif failure == "missing_sibling":
        broker.entry.legs = [broker.sl]
    elif failure == "cancel_error":
        broker.cancel_mode = "error"
    elif failure == "confirm_error":
        broker.cancel_mode = "read_error"
    else:
        broker.cancel_mode = "pending"
    run(broker)
    assert broker.submitted == []
    if failure not in {"cancel_pending", "cancel_error", "confirm_error"}:
        assert broker.canceled == []


def test_tp_filling_during_cancel_does_not_sell_again(lifecycle):
    trade_id, run = lifecycle
    broker = Broker(tp_qty=0, tp_status="new")
    broker.cancel_mode = "fill"
    trade = run(broker)
    assert broker.submitted == []
    assert trade["status"] == "closed"
    assert db.get_exit_fill_totals(trade_id) == (2, 220)


def test_canceled_partial_entry_uses_actual_owned_quantity(lifecycle):
    _, run = lifecycle
    broker = Broker(entry_qty=1, tp_qty=1)
    broker.entry.status = "canceled"
    trade = run(broker)
    assert broker.submitted == []
    assert trade["shares"] == 1
    assert trade["status"] == "closed"
    assert trade["pnl_dollars"] == 10


@pytest.mark.parametrize("status", ["canceled", "expired", "rejected"])
def test_terminal_zero_fill_entry_releases_slot_without_touching_foreign_shares(lifecycle, status):
    _, run = lifecycle
    broker = Broker(entry_qty=0, tp_qty=0, tp_status="canceled")
    broker.entry.status = status
    broker.entry.filled_avg_price = None
    broker.entry.legs = []
    trade = run(broker)
    assert trade["status"] == "closed"
    assert trade["exit_reason"] == "entry_not_filled"
    assert trade["shares"] == 0
    assert broker.submitted == broker.canceled == []


def test_rearmed_oco_fills_are_linked_and_not_sold_again(lifecycle):
    trade_id, run = lifecycle
    broker = Broker(tp_qty=0, tp_status="canceled")
    root = order("protect-1", "swingv2-protect-ensemble-AMD-own", "sell", 1, 110, "canceled")
    stop = order("protect-sl", "broker-new-stop", "sell", 1, 90, "filled")
    root.legs = [stop]
    broker.orders.update({root.id: root, stop.id: stop})
    db.set_protect_order_ids(trade_id, root.client_order_id, root.id)
    trade = run(broker)
    assert trade["status"] == "closed"
    assert trade["pnl_dollars"] == 0
    assert db.get_exit_fill_totals(trade_id) == (2, 200)
    assert broker.submitted == broker.canceled == []


def test_unreadable_rearmed_protection_blocks_exit(lifecycle):
    trade_id, run = lifecycle
    broker = Broker(tp_qty=0, tp_status="canceled")
    db.set_protect_order_ids(trade_id, "swingv2-protect-ensemble-AMD-own", "missing")
    run(broker)
    assert broker.submitted == broker.canceled == []


def test_partial_fill_during_cancel_is_subtracted_before_exit(lifecycle):
    trade_id, run = lifecycle
    broker = Broker(tp_qty=0, tp_status="new")
    broker.cancel_mode = "partial"
    run(broker)
    assert [o.qty for o in broker.submitted] == [1]
    assert db.get_exit_fill_totals(trade_id) == (1, 110)
