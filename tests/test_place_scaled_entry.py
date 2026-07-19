import pandas as pd
import pytest
from types import SimpleNamespace

import bot
from tests.fakes import FakeTradingClient
from strategy import EntrySignal


def _sig():
    return EntrySignal(date=pd.Timestamp("2026-01-02"), entry_price=100.0,
                       stop_loss=90.0, take_profit=108.0, atr=2.0, rsi=55.0,
                       strategy="ensemble")


def test_place_scaled_entry_orders():
    tc = FakeTradingClient()
    bot._place_scaled_entry(tc, "AMD", qty=9, sig=_sig(), strat_name="ensemble")
    types = [o.type for o in tc.submitted]
    # One atomic stop-protected market entry plus three independent TP sells.
    assert types.count("MarketOrderRequest") == 1
    assert types.count("LimitOrderRequest") == 3
    assert types.count("StopOrderRequest") == 0
    entry = tc.submitted[0]
    assert entry.order_class == "oto"
    assert entry.stop_loss.stop_price == 90.0
    sells = [o for o in tc.submitted if o.type == "LimitOrderRequest"]
    assert sorted(int(o.qty) for o in sells) == [3, 3, 3]
    # limit prices are tp1/tp2/tp3
    assert sorted(round(o.limit_price, 2) for o in sells) == [102.67, 105.33, 108.0]
    # every order is bot-owned
    assert all(o.client_order_id and o.client_order_id.startswith("swingv2") for o in tc.submitted)


class _FailingTargetClient(FakeTradingClient):
    def __init__(self, fail_on_submit):
        super().__init__()
        self.fail_on_submit = fail_on_submit
        self.submit_count = 0

    def submit_order(self, req):
        self.submit_count += 1
        if self.submit_count == self.fail_on_submit:
            raise RuntimeError(f"submit {self.submit_count} failed")
        return super().submit_order(req)

    def cancel_order_by_id(self, oid):
        super().cancel_order_by_id(oid)
        self.get_order_by_id(oid).status = "canceled"

    def get_order_by_id(self, oid):
        return next(order for order in self.submitted if order.id == oid)


@pytest.mark.parametrize("fail_on_submit", [2, 3, 4])
def test_scaled_target_failure_keeps_atomic_stop_and_records_entry_first(
    fail_on_submit,
):
    tc = _FailingTargetClient(fail_on_submit)
    accepted = []

    def on_entry_accepted(info):
        accepted.append((info["entry_coid"], len(tc.submitted)))

    result = bot._place_scaled_entry(
        tc,
        "AMD",
        qty=9,
        sig=_sig(),
        strat_name="ensemble",
        on_entry_accepted=on_entry_accepted,
    )

    assert accepted == [(result["entry_coid"], 1)]
    assert result["setup_error"] == f"submit {fail_on_submit} failed"
    assert result["cleanup_confirmed"] is True
    assert tc.submitted[0].order_class == "oto"
    assert tc.submitted[0].stop_loss.stop_price == 90.0
    submitted_targets = [
        order for order in tc.submitted if order.type == "LimitOrderRequest"
    ]
    assert tc.cancelled == [order.id for order in submitted_targets]


def test_scaled_target_cleanup_reports_unconfirmed_cancellation(monkeypatch):
    class _UnconfirmedClient(_FailingTargetClient):
        def cancel_order_by_id(self, oid):
            self.cancelled.append(oid)

    monkeypatch.setattr(bot.time, "sleep", lambda _delay: None)
    tc = _UnconfirmedClient(fail_on_submit=3)

    result = bot._place_scaled_entry(
        tc,
        "AMD",
        qty=9,
        sig=_sig(),
        strat_name="ensemble",
    )

    assert result["setup_error"] == "submit 3 failed"
    assert result["cleanup_confirmed"] is False


def test_open_stop_order_finds_attached_scaled_oto_stop():
    stop = SimpleNamespace(id="stop-1", status="held", stop_price=90.0)
    parent = SimpleNamespace(legs=[stop])

    class _NestedClient:
        def get_orders(self, filter=None):
            return []

        def get_order_by_id(self, order_id, filter=None):
            assert order_id == "entry-1"
            return parent

    trade = {
        "ticker": "AMD",
        "alpaca_order_id": "entry-1",
        "client_order_id": None,
    }

    assert bot._open_stop_order(_NestedClient(), trade) is stop


def test_stepped_stop_resizes_after_partial_target_fill(monkeypatch):
    stop = SimpleNamespace(id="stop-1", stop_price=90.0, qty=9)
    submitted = []

    class _Client:
        def __init__(self):
            self.cancelled = []

        def cancel_order_by_id(self, order_id):
            self.cancelled.append(order_id)

        def submit_order(self, request):
            submitted.append(request)

    client = _Client()
    trade = {
        "ticker": "AMD",
        "strategy": "ensemble",
        "entry_price": 100.0,
        "take_profit": 108.0,
        "stop_loss": 90.0,
    }
    monkeypatch.setattr(bot, "_count_filled_tp_legs", lambda *_args: 0)
    monkeypatch.setattr(bot, "_open_stop_order", lambda *_args: stop)
    monkeypatch.setattr(bot, "_position_qty", lambda *_args: 8.0)

    bot._sync_stepped_stop(client, trade)

    assert client.cancelled == ["stop-1"]
    assert len(submitted) == 1
    assert int(submitted[0].qty) == 8
    assert submitted[0].stop_price == 90.0
