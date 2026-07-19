"""Kill switch, market-clock gating, entry-fill recording, cross-strategy reconcile."""
from __future__ import annotations

from types import SimpleNamespace

import bot


# ── Kill switch ───────────────────────────────────────────────────────────────

class _KillSwitchClient:
    def __init__(self, equity, last_equity):
        self._equity = equity
        self._last = last_equity

    def get_account(self):
        return SimpleNamespace(
            equity=self._equity, cash=self._equity, last_equity=self._last
        )

    def get_all_positions(self):
        return []


def test_daily_loss_beyond_limit_disables_sizing_and_emails_once(tmp_path, monkeypatch):
    notified = []
    monkeypatch.setattr(bot, "_KILL_SWITCH_MARKER", tmp_path / "killswitch.date")
    monkeypatch.setattr(
        bot, "send_notification", lambda *args, **kwargs: notified.append(args)
    )
    client = _KillSwitchClient(equity="960", last_equity="1000")  # -4% day

    assert bot._load_live_sizing(client) is None
    assert bot._load_live_sizing(client) is None

    assert len(notified) == 1
    assert "kill switch" in notified[0][0].lower()


def test_daily_loss_within_limit_keeps_entries_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "_KILL_SWITCH_MARKER", tmp_path / "killswitch.date")
    monkeypatch.setattr(bot, "send_notification", lambda *args, **kwargs: None)
    client = _KillSwitchClient(equity="985", last_equity="1000")  # -1.5% day

    state = bot._load_live_sizing(client)

    assert state is not None
    assert state.equity == 985.0


def test_missing_last_equity_skips_the_kill_switch_check():
    assert bot._daily_loss_pct(SimpleNamespace(equity="1000")) is None


# ── Market clock gating ───────────────────────────────────────────────────────

def test_trading_hours_follow_the_broker_clock(monkeypatch):
    for is_open in (True, False):
        client = SimpleNamespace(get_clock=lambda o=is_open: SimpleNamespace(is_open=o))
        monkeypatch.setattr(bot, "_get_trading", lambda c=client: c)
        assert bot._in_trading_hours() is is_open


# ── Entry fill recording ──────────────────────────────────────────────────────

def test_record_entry_fill_persists_broker_average_price(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        bot.db_mod, "set_entry_fill", lambda *args: recorded.append(args)
    )
    order = SimpleNamespace(status="filled", filled_avg_price="101.37", filled_qty="3")
    tc = SimpleNamespace(get_order_by_id=lambda _id: order)

    bot._record_entry_fill(tc, 42, "alpaca-1")

    assert recorded == [(42, 101.37, 3.0)]


def test_record_entry_fill_gives_up_quietly_when_unfilled(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        bot.db_mod, "set_entry_fill", lambda *args: recorded.append(args)
    )
    order = SimpleNamespace(status="new", filled_avg_price=None, filled_qty="0")
    tc = SimpleNamespace(get_order_by_id=lambda _id: order)

    bot._record_entry_fill(tc, 42, "alpaca-1", attempts=2, delay=0.0)

    assert recorded == []


def test_backfill_records_fill_and_updates_trade_in_place(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        bot.db_mod, "set_entry_fill", lambda *args: recorded.append(args)
    )
    order = SimpleNamespace(status="filled", filled_avg_price="99.10", filled_qty="2")

    class _Client:
        def get_order_by_id(self, _order_id, **_kwargs):
            return order

        def get_order_by_client_id(self, _coid):
            return order

    trade = {
        "id": 7,
        "ticker": "AMD",
        "entry_price": 100.0,
        "alpaca_order_id": "entry-7",
        "client_order_id": "swingv2-entry-ensemble-AMD-x",
    }
    bot._backfill_entry_fill(_Client(), trade)

    assert recorded == [(7, 99.10, 2.0)]
    assert trade["entry_filled_price"] == 99.10
    assert bot._effective_entry_price(trade) == 99.10


def test_effective_entry_price_falls_back_to_signal_close():
    assert bot._effective_entry_price({"entry_price": 100.0}) == 100.0
    assert (
        bot._effective_entry_price({"entry_price": 100.0, "entry_filled_price": None})
        == 100.0
    )


# ── Cross-strategy reconciliation ─────────────────────────────────────────────

def test_reconcile_covers_trades_from_other_strategies(monkeypatch):
    """Restarting with a different strategy must not orphan older trades."""
    trade = {
        "id": 3,
        "ticker": "AMD",
        "strategy": "ensemble",
        "entry_date": "2026-06-01",
        "entry_price": 100.0,
        "shares": 2,
        "client_order_id": "swingv2-entry-ensemble-AMD-old",
        "alpaca_order_id": "entry-3",
        "created_at": "2026-06-01T14:30:00+00:00",
    }

    class _Client:
        def get_open_position(self, _ticker):
            return SimpleNamespace(qty="2", current_price="105")

    closed = []
    monkeypatch.setattr(bot, "_get_trading", lambda: _Client())
    monkeypatch.setattr(bot.db_mod, "get_open_trades", lambda: [trade])
    monkeypatch.setattr(bot, "_verify_owned", lambda *_: True)
    monkeypatch.setattr(bot, "_backfill_entry_fill", lambda *_: None)
    monkeypatch.setattr(bot, "_adopt_untracked_exit", lambda *_: False)
    monkeypatch.setattr(
        bot, "_close_owned",
        lambda *args, **kwargs: closed.append(kwargs["reason"]),
    )

    # Bot restarted under sma_50_cross; the ensemble trade is past max-hold
    # (entered 2026-06-01, today is later) and above breakeven.
    bot._reconcile_and_exit("sma_50_cross", {})

    assert closed == ["time_stop"]


def test_reconcile_leaves_unknown_strategy_trades_untouched(monkeypatch):
    trade = {
        "id": 4,
        "ticker": "AMD",
        "strategy": "retired_strategy",
        "entry_date": "2026-06-01",
        "entry_price": 100.0,
    }

    inspected = []
    monkeypatch.setattr(
        bot, "_get_trading", lambda: SimpleNamespace(
            get_open_position=lambda _t: inspected.append(_t)
        )
    )
    monkeypatch.setattr(bot.db_mod, "get_open_trades", lambda: [trade])

    bot._reconcile_and_exit("ensemble", {})

    assert inspected == []
