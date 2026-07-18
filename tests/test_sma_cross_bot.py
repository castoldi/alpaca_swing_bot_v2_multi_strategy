from types import SimpleNamespace

import pandas as pd

import bot
from config import PARAMS
from strategies.base import add_indicators
from tests.fakes import FakeTradingClient


def _open_sma_trade(**overrides):
    trade = {
        "id": 1,
        "ticker": "AMD",
        "strategy": "sma_50_cross",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "take_profit": 0.0,
        "shares": 1,
        "client_order_id": "swingv2-entry-sma_50_cross-AMD-abcd",
        "alpaca_order_id": "entry-1",
        "entry_date": "2026-07-01",
        "created_at": "2026-07-01T14:30:00+00:00",
    }
    trade.update(overrides)
    return trade


def test_place_stop_only_entry_submits_one_oto_with_no_take_profit():
    tc = FakeTradingClient()
    info = bot._place_stop_only_entry(tc, "AMD", 2, 90.0, "sma_50_cross")
    assert len(tc.submitted) == 1
    order = tc.submitted[0]
    assert order.order_class == "oto"
    assert order.stop_loss.stop_price == 90.0
    assert order.take_profit is None
    assert info["entry_coid"].startswith("swingv2-entry-sma_50_cross-AMD-")


def test_reconcile_closes_owned_sma_trade_on_cross_down(monkeypatch):
    closes = [100.0] * 49 + [101.0, 99.0]
    values = pd.Series(
        closes, index=pd.date_range("2026-01-01", periods=len(closes), freq="D")
    )
    frame = add_indicators(pd.DataFrame({
        "open": values,
        "high": values + 1,
        "low": values - 1,
        "close": values,
        "volume": 1_000_000,
    }), PARAMS)
    trade = _open_sma_trade()
    tc = SimpleNamespace(
        get_open_position=lambda _: SimpleNamespace(qty="1", current_price="99")
    )
    monkeypatch.setattr(bot, "_get_trading", lambda: tc)
    monkeypatch.setattr(
        bot.db_mod, "get_open_trades_by_strategy", lambda _: [trade]
    )
    monkeypatch.setattr(bot, "_verify_owned", lambda *_: True)
    closed = []
    monkeypatch.setattr(
        bot, "_close_owned",
        lambda *args, **kwargs: closed.append(kwargs["reason"]),
    )
    bot._reconcile_and_exit("sma_50_cross", {"AMD": frame})
    assert closed == ["sma_cross_down"]


def test_sma_exit_is_blocked_when_attached_stop_cannot_be_inspected(monkeypatch):
    monkeypatch.setattr(bot, "send_notification", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot.db_mod, "set_exit_intent", lambda *_args: None)

    class Client:
        submitted = []

        def get_order_by_id(self, *_args, **_kwargs):
            raise RuntimeError("broker unavailable")

        def submit_order(self, request):
            self.submitted.append(request)

    tc = Client()
    bot._close_owned(
        tc,
        _open_sma_trade(),
        SimpleNamespace(qty="1", current_price="99"),
        "sma_cross_down",
    )
    assert tc.submitted == []


def test_sma_exit_is_blocked_when_attached_stop_cancellation_fails(monkeypatch):
    monkeypatch.setattr(bot, "send_notification", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot.db_mod, "set_exit_intent", lambda *_args: None)

    stop = SimpleNamespace(id="stop-1", status="held")

    class Client:
        submitted = []

        def get_order_by_id(self, *_args, **_kwargs):
            return SimpleNamespace(legs=[stop])

        def cancel_order_by_id(self, _order_id):
            raise RuntimeError("cancel rejected")

        def submit_order(self, request):
            self.submitted.append(request)

    tc = Client()
    bot._close_owned(
        tc,
        _open_sma_trade(),
        SimpleNamespace(qty="1", current_price="99"),
        "sma_cross_down",
    )
    assert tc.submitted == []


def test_unfilled_sma_exit_stays_open_and_records_pending_order(monkeypatch):
    stop = SimpleNamespace(id="stop-1", status="held")

    class Client:
        def __init__(self):
            self.submitted = []

        def get_order_by_id(self, order_id, **_kwargs):
            if order_id == "entry-1":
                return SimpleNamespace(legs=[stop])
            return stop

        def cancel_order_by_id(self, _order_id):
            stop.status = "canceled"

        def get_open_position(self, _ticker):
            return SimpleNamespace(qty="1", current_price="99")

        def submit_order(self, request):
            self.submitted.append(request)
            return SimpleNamespace(
                id="exit-1",
                status="new",
                client_order_id=request.client_order_id,
            )

    pending = []
    closed = []
    monkeypatch.setattr(
        bot.db_mod,
        "set_exit_pending",
        lambda *args: pending.append(args),
        raising=False,
    )
    monkeypatch.setattr(
        bot.db_mod, "close_trade", lambda *args, **kwargs: closed.append((args, kwargs))
    )
    monkeypatch.setattr(bot.db_mod, "set_exit_intent", lambda *_args: None)
    monkeypatch.setattr(bot.db_mod, "get_exit_fill_totals", lambda _id: (0.0, 0.0))
    monkeypatch.setattr(bot.db_mod, "record_exit_order_progress", lambda *_args: None)
    monkeypatch.setattr(bot, "send_notification", lambda *_args, **_kwargs: None)

    tc = Client()
    bot._close_owned(
        tc,
        _open_sma_trade(),
        SimpleNamespace(qty="1", current_price="99"),
        "sma_cross_down",
    )

    assert len(tc.submitted) == 1
    assert pending and pending[0][0] == 1 and pending[0][2] == "exit-1"
    assert closed == []


def test_pending_sma_exit_closes_only_at_confirmed_broker_fill(monkeypatch):
    trade = _open_sma_trade(
        exit_client_order_id="swingv2-exit-sma_50_cross-AMD-efgh",
        exit_alpaca_order_id="exit-1",
    )
    exit_order = SimpleNamespace(
        id="exit-1",
        status="filled",
        filled_avg_price="98.50",
        filled_qty="1",
        client_order_id=trade["exit_client_order_id"],
    )

    class Client:
        def get_open_position(self, _ticker):
            return SimpleNamespace(qty="1", current_price="99")

        def get_order_by_id(self, order_id, **_kwargs):
            assert order_id == "exit-1"
            return exit_order

    closed = []
    monkeypatch.setattr(bot, "_get_trading", lambda: Client())
    monkeypatch.setattr(bot.db_mod, "get_open_trades_by_strategy", lambda _: [trade])
    monkeypatch.setattr(
        bot.db_mod, "close_trade", lambda *args, **kwargs: closed.append((args, kwargs))
    )
    monkeypatch.setattr(bot.db_mod, "record_exit_order_progress", lambda *_args: None)
    monkeypatch.setattr(bot.db_mod, "get_exit_fill_totals", lambda _id: (1.0, 98.5))
    monkeypatch.setattr(bot, "send_notification", lambda *_args, **_kwargs: None)

    bot._reconcile_and_exit("sma_50_cross", {"AMD": pd.DataFrame()})

    assert len(closed) == 1
    args, kwargs = closed[0]
    assert args[2] == 98.5
    assert args[3] == "sma_cross_down"
    assert kwargs["exit_alpaca_order_id"] == "exit-1"


def test_restart_adopts_owned_exit_submitted_before_pending_state_was_saved(monkeypatch):
    trade = _open_sma_trade()
    exit_order = SimpleNamespace(
        id="exit-2",
        status="new",
        client_order_id="swingv2-exit-sma_50_cross-AMD-restart",
        submitted_at="2026-07-18T14:30:00+00:00",
    )

    class Client:
        def get_open_position(self, _ticker):
            return SimpleNamespace(qty="1", current_price="99")

    pending = []
    monkeypatch.setattr(bot, "_get_trading", lambda: Client())
    monkeypatch.setattr(bot.db_mod, "get_open_trades_by_strategy", lambda _: [trade])
    monkeypatch.setattr(bot, "_our_sell_orders", lambda *_args: [exit_order])
    monkeypatch.setattr(
        bot.db_mod, "set_exit_pending", lambda *args: pending.append(args)
    )

    bot._reconcile_and_exit("sma_50_cross", {"AMD": pd.DataFrame()})

    assert pending == [
        (
            1,
            "swingv2-exit-sma_50_cross-AMD-restart",
            "exit-2",
        )
    ]


def test_exit_intent_is_persisted_before_stop_cancel_and_survives_submit_failure(monkeypatch):
    stop = SimpleNamespace(
        id="stop-1",
        status="held",
        filled_qty="0",
        filled_avg_price=None,
        client_order_id="broker-stop",
    )

    class Client:
        def get_order_by_id(self, order_id, **_kwargs):
            if order_id == "entry-1":
                return SimpleNamespace(legs=[stop])
            return stop

        def cancel_order_by_id(self, _order_id):
            stop.status = "canceled"

        def get_open_position(self, _ticker):
            return SimpleNamespace(qty="1", current_price="99")

        def submit_order(self, _request):
            raise RuntimeError("submit unavailable")

    intents = []
    monkeypatch.setattr(
        bot.db_mod, "set_exit_intent", lambda *args: intents.append(args), raising=False
    )
    monkeypatch.setattr(
        bot.db_mod, "record_exit_order_progress", lambda *_args: None, raising=False
    )
    monkeypatch.setattr(bot.db_mod, "get_exit_fill_totals", lambda _id: (0.0, 0.0), raising=False)

    result = bot._close_owned(
        Client(),
        _open_sma_trade(),
        SimpleNamespace(qty="1", current_price="99"),
        "sma_cross_down",
    )

    assert result is False
    assert len(intents) == 1
    assert intents[0][0] == 1
    assert intents[0][1] == "sma_cross_down"


def test_stop_partial_fill_is_recorded_and_exit_quantity_is_refreshed(monkeypatch):
    stop = SimpleNamespace(
        id="stop-1",
        status="partially_filled",
        filled_qty="1",
        filled_avg_price="90",
        client_order_id="broker-stop",
    )

    class Client:
        def __init__(self):
            self.submitted = []

        def get_order_by_id(self, order_id, **_kwargs):
            if order_id == "entry-1":
                return SimpleNamespace(legs=[stop])
            return stop

        def cancel_order_by_id(self, _order_id):
            stop.status = "canceled"

        def get_open_position(self, _ticker):
            return SimpleNamespace(qty="1", current_price="99")

        def submit_order(self, request):
            self.submitted.append(request)
            return SimpleNamespace(
                id="exit-remaining",
                status="new",
                client_order_id=request.client_order_id,
                filled_qty="0",
            )

    progress = []
    monkeypatch.setattr(bot.db_mod, "set_exit_intent", lambda *_args: None, raising=False)
    monkeypatch.setattr(
        bot.db_mod,
        "record_exit_order_progress",
        lambda *args: progress.append(args),
        raising=False,
    )
    monkeypatch.setattr(bot.db_mod, "get_exit_fill_totals", lambda _id: (1.0, 90.0), raising=False)
    monkeypatch.setattr(bot.db_mod, "set_exit_pending", lambda *_args: None)

    tc = Client()
    bot._close_owned(
        tc,
        _open_sma_trade(shares=2),
        SimpleNamespace(qty="2", current_price="99"),
        "sma_cross_down",
    )

    assert progress and progress[0][1] == "stop-1"
    assert len(tc.submitted) == 1
    assert int(tc.submitted[0].qty) == 1


def test_terminal_partial_exit_records_fill_and_retries_only_later(monkeypatch):
    trade = _open_sma_trade(
        shares=2,
        exit_intent_reason="sma_cross_down",
        exit_client_order_id="swingv2-exit-sma_50_cross-AMD-partial",
        exit_alpaca_order_id="exit-partial",
    )
    order = SimpleNamespace(
        id="exit-partial",
        status="canceled",
        filled_qty="1",
        filled_avg_price="99",
        client_order_id=trade["exit_client_order_id"],
    )

    class Client:
        def get_order_by_id(self, _order_id):
            return order

    progress = []
    cleared = []
    closed = []
    monkeypatch.setattr(
        bot.db_mod,
        "record_exit_order_progress",
        lambda *args: progress.append(args),
        raising=False,
    )
    monkeypatch.setattr(bot.db_mod, "get_exit_fill_totals", lambda _id: (1.0, 99.0), raising=False)
    monkeypatch.setattr(bot.db_mod, "clear_exit_pending", lambda trade_id: cleared.append(trade_id))
    monkeypatch.setattr(bot.db_mod, "close_trade", lambda *args, **kwargs: closed.append(args))

    assert bot._reconcile_pending_exit(Client(), trade) is True
    assert progress and progress[0][1] == "exit-partial"
    assert cleared == [1]
    assert closed == []


def test_durable_exit_intent_retries_after_cross_is_no_longer_latest(monkeypatch):
    stop = SimpleNamespace(
        id="stop-1",
        status="canceled",
        filled_qty="0",
        filled_avg_price=None,
        client_order_id="broker-stop",
    )
    trade = _open_sma_trade(
        exit_intent_reason="sma_cross_down",
        exit_client_order_id="swingv2-exit-sma_50_cross-AMD-retry",
        exit_alpaca_order_id=None,
    )

    class Client:
        def __init__(self):
            self.submitted = []

        def get_open_position(self, _ticker):
            return SimpleNamespace(qty="1", current_price="99")

        def get_order_by_client_id(self, _coid):
            raise RuntimeError("not found")

        def get_order_by_id(self, order_id, **_kwargs):
            if order_id == "entry-1":
                return SimpleNamespace(legs=[stop])
            return stop

        def submit_order(self, request):
            self.submitted.append(request)
            return SimpleNamespace(
                id="exit-retry",
                status="new",
                filled_qty="0",
                client_order_id=request.client_order_id,
            )

    client = Client()
    pending = []
    monkeypatch.setattr(bot, "_get_trading", lambda: client)
    monkeypatch.setattr(bot.db_mod, "get_open_trades_by_strategy", lambda _: [trade])
    monkeypatch.setattr(bot.db_mod, "record_exit_order_progress", lambda *_args: None)
    monkeypatch.setattr(bot.db_mod, "get_exit_fill_totals", lambda _id: (0.0, 0.0))
    monkeypatch.setattr(bot.db_mod, "set_exit_pending", lambda *args: pending.append(args))

    bot._reconcile_and_exit("sma_50_cross", {"AMD": pd.DataFrame()})

    assert len(client.submitted) == 1
    assert client.submitted[0].client_order_id == trade["exit_client_order_id"]
    assert pending[0][2] == "exit-retry"


def test_completed_owned_fills_finalize_without_selling_a_residual_position(monkeypatch):
    stop = SimpleNamespace(
        id="stop-1",
        status="canceled",
        filled_qty="1",
        filled_avg_price="98",
        client_order_id="broker-stop",
    )
    trade = _open_sma_trade(
        shares=1,
        exit_intent_reason="sma_cross_down",
        exit_client_order_id="swingv2-exit-sma_50_cross-AMD-complete",
    )

    class Client:
        def __init__(self):
            self.submitted = []

        def get_order_by_id(self, order_id, **_kwargs):
            if order_id == "entry-1":
                return SimpleNamespace(legs=[stop])
            return stop

        def get_open_position(self, _ticker):
            # This residual share is outside the bot-owned quantity already exited.
            return SimpleNamespace(qty="1", current_price="99")

        def submit_order(self, request):
            self.submitted.append(request)

    closed = []
    monkeypatch.setattr(bot.db_mod, "record_exit_order_progress", lambda *_args: None)
    monkeypatch.setattr(bot.db_mod, "get_exit_fill_totals", lambda _id: (1.0, 98.0))
    monkeypatch.setattr(
        bot.db_mod, "close_trade", lambda *args, **kwargs: closed.append((args, kwargs))
    )
    monkeypatch.setattr(bot, "send_notification", lambda *_args, **_kwargs: None)

    client = Client()
    bot._execute_exit_intent(
        client,
        trade,
        SimpleNamespace(qty="1", current_price="99"),
        "sma_cross_down",
        trade["exit_client_order_id"],
    )

    assert len(closed) == 1
    assert client.submitted == []
