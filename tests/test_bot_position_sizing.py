from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pandas as pd

import bot
from config import StrategyType
from strategies.base import EntrySignal


class _NoPositionError(RuntimeError):
    """Alpaca's 404 for get_open_position on a symbol with no position."""
    status_code = 404


class _AccountClient:
    def __init__(
        self,
        equity="1000",
        cash="1000",
        fail_account=False,
        open_positions=0,
    ):
        self.equity = equity
        self.cash = cash
        self.fail_account = fail_account
        self.open_positions = open_positions

    def get_account(self):
        if self.fail_account:
            raise RuntimeError("account unavailable")
        return SimpleNamespace(equity=self.equity, cash=self.cash)

    def get_open_position(self, _ticker):
        raise _NoPositionError("position does not exist")

    def get_all_positions(self):
        return [SimpleNamespace(symbol=f"OPEN{index}") for index in range(self.open_positions)]

    def get_order_by_id(self, _order_id):
        raise RuntimeError("order lookup unavailable")


class _SignalStrategy:
    name = "ensemble"
    timeframe = "4h"
    has_take_profit = True
    exit_mode = "bracket"

    @staticmethod
    def check_entry(frame, idx, params):
        return EntrySignal(
            date=pd.Timestamp(frame.index[idx]),
            entry_price=100.0,
            stop_loss=90.0,
            take_profit=110.0,
            atr=10.0,
            rsi=55.0,
            strategy="ensemble",
        )


def _frame():
    index = pd.date_range("2026-01-01", periods=60, freq="4h")
    return pd.DataFrame(
        {
            "open": [100.0] * 60,
            "high": [101.0] * 60,
            "low": [99.0] * 60,
            "close": [100.0] * 60,
            "volume": [1_000_000.0] * 60,
        },
        index=index,
    )


def _configure_cycle(monkeypatch, client, tickers, prices):
    placed = []
    notified = []
    reconciled = []
    monkeypatch.setitem(bot.REGISTRY, "ensemble", _SignalStrategy())
    monkeypatch.setattr(bot, "TICKERS", list(tickers))
    monkeypatch.setattr(bot, "_get_trading", lambda: client)
    monkeypatch.setattr(bot, "fetch_bars", lambda *_args, **_kwargs: _frame())
    monkeypatch.setattr(bot.data_feed, "completed_bars", lambda data, _: data)
    monkeypatch.setattr(bot, "add_indicators", lambda data, _: data)
    monkeypatch.setattr(bot, "is_tp_reachable_in_days", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        bot.data_feed,
        "fetch_snapshots",
        lambda symbols: {symbols[0]: {"price": prices[symbols[0]]}},
    )
    monkeypatch.setattr(bot.db_mod, "start_bot_run", lambda *_args: 1)
    monkeypatch.setattr(bot.db_mod, "finish_bot_run", lambda *_args: None)
    monkeypatch.setattr(bot.db_mod, "get_open_trade", lambda *_args: None)
    monkeypatch.setattr(bot.db_mod, "get_open_trades", lambda: [])
    monkeypatch.setattr(bot.db_mod, "save_trade", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bot.bot_hooks, "log_signal", lambda *_args: None)

    def place_single(
        _tc,
        ticker,
        qty,
        _sig,
        _name,
        entry_coid=None,
    ):
        placed.append((ticker, qty))
        return {
            "entry_coid": entry_coid or f"coid-{ticker}",
            "alpaca_id": f"id-{ticker}",
        }

    monkeypatch.setattr(bot, "_place_single_bracket_entry", place_single)
    monkeypatch.setattr(
        bot, "send_notification",
        lambda *args, **kwargs: notified.append((args, kwargs)),
    )
    monkeypatch.setattr(
        bot, "_reconcile_and_exit",
        lambda *args, **kwargs: reconciled.append((args, kwargs)),
    )
    return placed, notified, reconciled


def test_load_live_sizing_reads_equity_and_cash():
    state = bot._load_live_sizing(_AccountClient(equity="1234.50", cash="456.75"))

    assert state.equity == 1234.50
    assert state.remaining_cash == 456.75
    assert state.remaining_slots == 5


def test_live_cycle_uses_snapshot_price_for_twenty_percent_quantity(monkeypatch):
    placed, _, _ = _configure_cycle(
        monkeypatch,
        _AccountClient(equity="1500", cash="1500"),
        ["TEST"],
        {"TEST": 100.0},
    )

    bot.run_once(StrategyType.ENSEMBLE)

    assert placed == [("TEST", 3)]


def test_live_cycle_caps_second_signal_by_locally_remaining_cash(monkeypatch):
    placed, _, _ = _configure_cycle(
        monkeypatch,
        _AccountClient(equity="1500", cash="450"),
        ["A", "B"],
        {"A": 100.0, "B": 100.0},
    )

    bot.run_once(StrategyType.ENSEMBLE)

    assert placed == [("A", 3), ("B", 1)]


def test_high_price_skip_submits_and_notifies_nothing(monkeypatch):
    placed, notified, _ = _configure_cycle(
        monkeypatch,
        _AccountClient(equity="400", cash="400"),
        ["TEST"],
        {"TEST": 100.0},
    )

    bot.run_once(StrategyType.ENSEMBLE)

    assert placed == []
    assert notified == []


def test_slippage_guard_skips_price_far_from_signal(monkeypatch):
    placed, notified, _ = _configure_cycle(
        monkeypatch,
        _AccountClient(equity="1500", cash="1500"),
        ["TEST"],
        {"TEST": 105.0},  # 5% above the $100 signal close
    )

    bot.run_once(StrategyType.ENSEMBLE)

    assert placed == []
    assert notified == []


def test_position_lookup_failure_fails_closed(monkeypatch):
    class _OutageClient(_AccountClient):
        def get_open_position(self, _ticker):
            raise ConnectionError("broker unreachable")  # not a 404

    placed, notified, _ = _configure_cycle(
        monkeypatch,
        _OutageClient(equity="1500", cash="1500"),
        ["TEST"],
        {"TEST": 100.0},
    )

    bot.run_once(StrategyType.ENSEMBLE)

    assert placed == []
    assert notified == []


def test_account_failure_disables_entries_but_still_reconciles(monkeypatch):
    placed, _, reconciled = _configure_cycle(
        monkeypatch,
        _AccountClient(fail_account=True),
        ["TEST"],
        {"TEST": 100.0},
    )

    bot.run_once(StrategyType.ENSEMBLE)

    assert placed == []
    assert len(reconciled) == 1


def test_live_cycle_never_exceeds_five_account_positions(monkeypatch):
    placed, _, reconciled = _configure_cycle(
        monkeypatch,
        _AccountClient(open_positions=4),
        ["A", "B"],
        {"A": 100.0, "B": 100.0},
    )

    bot.run_once(StrategyType.ENSEMBLE)

    assert placed == [("A", 2)]
    assert len(reconciled) == 1


def test_unfilled_entry_from_prior_cycle_blocks_new_cycle_capacity(monkeypatch):
    class _UnfilledClient(_AccountClient):
        def get_order_by_client_id(self, coid):
            return SimpleNamespace(
                id="entry-A",
                client_order_id=coid,
                status="new",
                filled_qty="0",
                qty="1",
            )

    placed, _, _ = _configure_cycle(
        monkeypatch,
        _UnfilledClient(),
        ["A"],
        {"A": 101.0, "B": 101.0},
    )
    prior_entries = []
    monkeypatch.setattr(bot.db_mod, "get_open_trades", lambda: list(prior_entries))

    bot.run_once(StrategyType.ENSEMBLE)
    prior_entries.append(
        {
            "id": 11,
            "ticker": "A",
            "strategy": "ensemble",
            "entry_state": "accepted",
            "client_order_id": "swingv2-entry-ensemble-A-existing",
            "alpaca_order_id": "entry-A",
        }
    )
    monkeypatch.setattr(bot, "TICKERS", ["B"])

    bot.run_once(StrategyType.ENSEMBLE)

    assert placed == [("A", 1)]


def test_persistence_failure_prevents_broker_submission(monkeypatch):
    placed, _, reconciled = _configure_cycle(
        monkeypatch,
        _AccountClient(open_positions=4),
        ["A", "B"],
        {"A": 101.0, "B": 101.0},
    )

    def fail_save(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(bot.db_mod, "save_trade", fail_save)

    bot.run_once(StrategyType.ENSEMBLE)

    assert placed == []
    assert len(reconciled) == 1


def test_broker_rejection_closes_durable_entry_intent(monkeypatch):
    placed, _, _ = _configure_cycle(
        monkeypatch,
        _AccountClient(),
        ["TEST"],
        {"TEST": 101.0},
    )
    closed = []
    monkeypatch.setattr(bot.db_mod, "save_trade", lambda *_args, **_kwargs: 42)
    monkeypatch.setattr(
        bot.db_mod,
        "close_trade",
        lambda *args, **kwargs: closed.append((args, kwargs)),
    )

    class _RejectedEntry(Exception):
        status_code = 422

    def reject_order(*_args, **_kwargs):
        raise _RejectedEntry("broker rejected entry")

    monkeypatch.setattr(bot, "_place_single_bracket_entry", reject_order)

    bot.run_once(StrategyType.ENSEMBLE)

    assert placed == []
    assert len(closed) == 1
    assert closed[0][0][0] == 42
    assert closed[0][0][3] == "entry_not_submitted"


def test_ambiguous_submit_adopts_order_by_client_id(monkeypatch):
    class _AcceptedAfterTimeoutClient(_AccountClient):
        def get_order_by_client_id(self, coid):
            return SimpleNamespace(id="accepted-42", client_order_id=coid)

    client = _AcceptedAfterTimeoutClient()
    _configure_cycle(monkeypatch, client, ["TEST"], {"TEST": 101.0})
    attached = []
    closed = []
    finished = []
    monkeypatch.setattr(bot.db_mod, "save_trade", lambda *_args, **_kwargs: 42)
    monkeypatch.setattr(
        bot.db_mod,
        "set_entry_order_id",
        lambda *args: attached.append(args),
    )
    monkeypatch.setattr(
        bot.db_mod,
        "close_trade",
        lambda *args, **kwargs: closed.append((args, kwargs)),
    )
    monkeypatch.setattr(
        bot.db_mod,
        "finish_bot_run",
        lambda *args, **kwargs: finished.append((args, kwargs)),
    )
    monkeypatch.setattr(
        bot,
        "_place_single_bracket_entry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            TimeoutError("submit response timed out")
        ),
    )

    bot.run_once(StrategyType.ENSEMBLE)

    assert attached == [(42, "accepted-42")]
    assert closed == []
    assert finished[0][0][2] == 1


def test_ambiguous_submit_stays_pending_and_disables_entries(monkeypatch):
    class _UnknownClient(_AccountClient):
        def get_order_by_client_id(self, _coid):
            raise ConnectionError("broker unavailable")

    client = _UnknownClient()
    placed, _, reconciled = _configure_cycle(
        monkeypatch,
        client,
        ["A", "B"],
        {"A": 101.0, "B": 101.0},
    )
    closed = []
    monkeypatch.setattr(bot.db_mod, "save_trade", lambda *_args, **_kwargs: 42)
    monkeypatch.setattr(
        bot.db_mod,
        "close_trade",
        lambda *args, **kwargs: closed.append((args, kwargs)),
    )
    monkeypatch.setattr(
        bot,
        "_place_single_bracket_entry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            TimeoutError("submit response timed out")
        ),
    )

    bot.run_once(StrategyType.ENSEMBLE)

    assert placed == []
    assert closed == []
    assert len(reconciled) >= 2


def test_reconciliation_adopts_pending_entry_by_client_id(monkeypatch):
    attached = []
    order = SimpleNamespace(id="entry-99")
    monkeypatch.setattr(
        bot,
        "_lookup_entry_by_client_id",
        lambda *_args, **_kwargs: ("found", order),
    )
    monkeypatch.setattr(
        bot.db_mod,
        "set_entry_order_id",
        lambda *args: attached.append(args),
    )
    trade = {
        "id": 7,
        "ticker": "AMD",
        "entry_state": "pending_submission",
        "client_order_id": "swingv2-entry-ensemble-AMD-abcd",
    }

    assert bot._resolve_pending_entry(object(), trade) is True
    assert attached == [(7, "entry-99")]
    assert trade["entry_state"] == "accepted"


def test_reconciliation_retires_aged_broker_absent_intent(monkeypatch):
    closed = []
    monkeypatch.setattr(
        bot,
        "_lookup_entry_by_client_id",
        lambda *_args, **_kwargs: ("not_found", None),
    )
    monkeypatch.setattr(
        bot.db_mod,
        "close_trade",
        lambda *args, **kwargs: closed.append((args, kwargs)),
    )
    created_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    trade = {
        "id": 8,
        "ticker": "AMD",
        "entry_state": "pending_submission",
        "client_order_id": "swingv2-entry-ensemble-AMD-efgh",
        "created_at": created_at.isoformat(),
        "entry_price": 100.0,
    }

    assert bot._resolve_pending_entry(object(), trade) is False
    assert len(closed) == 1
    assert closed[0][0][3] == "entry_not_submitted"
