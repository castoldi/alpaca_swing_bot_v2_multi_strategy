"""Regressions for docs/code-review-2026-09-21-verification.md (V01-V03, V07)."""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

import bot
from config import StrategyType
from strategies.base import EntrySignal


class _NoPositionError(RuntimeError):
    status_code = 404


class _Client:
    def get_account(self):
        return SimpleNamespace(equity="10000", cash="10000")

    def get_open_position(self, _ticker):
        raise _NoPositionError("position does not exist")

    def get_all_positions(self):
        return []

    def get_order_by_id(self, _order_id):
        raise RuntimeError("order lookup unavailable")


class _Strategy:
    name = "ensemble"
    timeframe = "4h"
    has_take_profit = True
    exit_mode = "bracket"

    def __init__(self, signal_indexes=None):
        self.signal_indexes = signal_indexes  # None = every bar signals

    def check_entry(self, frame, idx, params):
        if self.signal_indexes is not None and idx not in self.signal_indexes:
            return None
        return EntrySignal(date=pd.Timestamp(frame.index[idx]), entry_price=100.0,
                           stop_loss=90.0, take_profit=110.0, atr=10.0, rsi=55.0,
                           strategy="ensemble")


def _frame(periods=60):
    index = pd.date_range("2026-01-01", periods=periods, freq="4h")
    return pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                         "volume": 1_000_000.0}, index=index)


def _cycle(monkeypatch, strategy, state):
    """state: dict with 'frame' and 'price' that tests mutate between cycles."""
    placed = []
    monkeypatch.setitem(bot.REGISTRY, strategy.name, strategy)
    monkeypatch.setattr(bot, "TICKERS", ["TEST"])
    monkeypatch.setattr(bot, "_get_trading", lambda: _Client())
    monkeypatch.setattr(bot, "fetch_bars", lambda *_a, **_k: state["frame"])
    monkeypatch.setattr(bot.data_feed, "completed_bars", lambda data, _: data)
    monkeypatch.setattr(bot, "add_indicators", lambda data, _: data)
    monkeypatch.setattr(bot, "is_tp_reachable_in_days", lambda *_a, **_k: True)
    monkeypatch.setattr(bot.data_feed, "fetch_snapshots",
                        lambda symbols: {symbols[0]: {"price": state["price"]}})
    monkeypatch.setattr(bot.db_mod, "get_open_trade", lambda *_a: None)  # prior trade exited
    monkeypatch.setattr(bot.db_mod, "get_open_trades", lambda: [])
    monkeypatch.setattr(bot.db_mod, "save_trade", lambda *_a, **_k: None)
    monkeypatch.setattr(bot.bot_hooks, "log_signal", lambda *_a: None)
    monkeypatch.setattr(bot, "_reconcile_and_exit", lambda *_a, **_k: [])

    def bracket(_tc, ticker, qty, sig, _name, entry_coid=None):
        placed.append((ticker, qty, pd.Timestamp(sig.date), sig.stop_loss))
        return {"entry_coid": entry_coid or "coid", "alpaca_id": "id"}

    def stop_only(_tc, ticker, qty, stop, _name, entry_coid=None):
        placed.append((ticker, qty, None, stop))
        return {"entry_coid": entry_coid or "coid", "alpaca_id": "id"}

    monkeypatch.setattr(bot, "_place_single_bracket_entry", bracket)
    monkeypatch.setattr(bot, "_place_stop_only_entry", stop_only)
    return placed


# ── V01: one evaluation per signal bar ────────────────────────────────────────

def test_same_bar_is_not_re_entered_after_the_trade_exits(monkeypatch):
    state = {"frame": _frame(), "price": 100.0}
    placed = _cycle(monkeypatch, _Strategy(), state)

    bot.run_once(StrategyType.ENSEMBLE)
    bot.run_once(StrategyType.ENSEMBLE)  # same latest bar, no open trade any more

    assert len(placed) == 1


def test_slippage_skip_is_not_retried_later_on_the_same_bar(monkeypatch):
    state = {"frame": _frame(), "price": 103.0}   # 3% drift > 1.5% guard
    placed = _cycle(monkeypatch, _Strategy(), state)

    bot.run_once(StrategyType.ENSEMBLE)
    state["price"] = 100.5                         # back in range, same bar
    bot.run_once(StrategyType.ENSEMBLE)

    assert placed == []


def test_a_newly_completed_bar_is_still_evaluated(monkeypatch):
    state = {"frame": _frame(60), "price": 100.0}
    placed = _cycle(monkeypatch, _Strategy(), state)

    bot.run_once(StrategyType.ENSEMBLE)
    state["frame"] = _frame(61)
    bot.run_once(StrategyType.ENSEMBLE)

    assert [p[2] for p in placed] == [_frame(60).index[-1], _frame(61).index[-1]]


def test_cursor_that_cannot_be_stored_blocks_the_entry(monkeypatch):
    state = {"frame": _frame(), "price": 100.0}
    placed = _cycle(monkeypatch, _Strategy(), state)

    def broken(*_a):
        raise RuntimeError("db locked")
    monkeypatch.setattr(bot.db_mod, "set_signal_cursor", broken)
    bot.run_once(StrategyType.ENSEMBLE)

    assert placed == []


# ── V02: bars completing while the market is closed are not skipped ──────────

def test_an_older_unevaluated_bar_signal_is_used_when_the_latest_bar_is_quiet(monkeypatch):
    state = {"frame": _frame(60), "price": 100.0}
    strategy = _Strategy(signal_indexes=set())
    placed = _cycle(monkeypatch, strategy, state)
    bot.run_once(StrategyType.ENSEMBLE)            # sets the cursor at bar 59

    # Overnight: the close bar (60) signals, the after-hours bar (61) does not.
    state["frame"] = _frame(62)
    strategy.signal_indexes = {60}
    bot.run_once(StrategyType.ENSEMBLE)

    assert [p[2] for p in placed] == [_frame(62).index[60]]


def test_first_run_without_a_cursor_does_not_replay_history(monkeypatch):
    state = {"frame": _frame(60), "price": 100.0}
    placed = _cycle(monkeypatch, _Strategy(signal_indexes={10, 20}), state)

    bot.run_once(StrategyType.ENSEMBLE)

    assert placed == []


@pytest.mark.real_signal_window
@pytest.mark.parametrize("bar,timeframe,now,expected", [
    # Friday 12:00-16:00 ET close bar -> fills Monday's open (13:30 UTC, EDT).
    ("2026-09-18 16:00", "4h", "2026-09-21 13:45", True),
    ("2026-09-18 16:00", "4h", "2026-09-21 14:10", False),   # > interval + grace
    ("2026-09-18 16:00", "4h", "2026-09-18 20:30", False),   # before the fill
    # Intraday bar completes at 16:00 UTC, inside the session.
    ("2026-09-21 12:00", "4h", "2026-09-21 16:20", True),
    ("2026-09-21 12:00", "4h", "2026-09-21 20:00", False),
    # Daily bar: available at NY midnight, fills at the next open.
    ("2026-09-18 04:00", "1d", "2026-09-21 13:50", True),
    ("2026-09-18 04:00", "1d", "2026-09-22 13:50", False),
])
def test_signal_window_matches_backtest_fill_time(bar, timeframe, now, expected):
    assert bot._signal_is_actionable(pd.Timestamp(bar), timeframe, now=pd.Timestamp(now)) is expected


@pytest.mark.real_signal_window
def test_stale_signal_outside_its_window_is_skipped_in_a_cycle(monkeypatch):
    state = {"frame": _frame(), "price": 100.0}   # bars from January 2026
    placed = _cycle(monkeypatch, _Strategy(), state)

    bot.run_once(StrategyType.ENSEMBLE)

    assert placed == []


def test_signal_exit_uses_any_bar_after_the_entry_signal(monkeypatch):
    frame = _frame(10)
    calls = []

    class Exit:
        def check_exit(self, _frame, idx, _params):
            calls.append(idx)
            return "cross_down" if idx == 7 else None

    trade = {"entry_date": str(frame.index[5])}
    assert bot._signal_exit_reason(Exit(), frame, trade) == "cross_down"
    assert min(calls) == 6                      # never the signal bar or earlier
    assert bot._signal_exit_reason(Exit(), frame, {"entry_date": str(frame.index[8])}) is None


# ── V03: each signal strategy's own emergency stop ───────────────────────────

def test_tqqq_live_emergency_stop_uses_its_own_eight_percent(monkeypatch):
    from strategies.tqqq_momentum import TQQQMomentumStrategy

    class Tqqq(_Strategy):
        name = "tqqq_momentum"
        has_take_profit = False
        exit_mode = "signal_with_stop"
        stop_loss_fraction = TQQQMomentumStrategy.stop_loss_fraction

    state = {"frame": _frame(), "price": 50.0}
    placed = _cycle(monkeypatch, Tqqq(), state)
    monkeypatch.setattr(bot, "LEVERAGED_TICKERS", [])

    bot.run_once(StrategyType.TQQQ_MOMENTUM)

    assert len(placed) == 1
    assert placed[0][3] == pytest.approx(50.0 * (1 - bot.PARAMS.tqqq_stop_loss_pct))
    assert bot.PARAMS.tqqq_stop_loss_pct != bot.PARAMS.sma_cross_stop_loss_pct


# ── V07: operator states are alerted, once per day ───────────────────────────

def test_alert_once_emails_once_per_key_per_day(monkeypatch):
    sent = []
    monkeypatch.setattr(bot, "send_notification", lambda *a, **k: sent.append(a))

    assert bot._alert_once("k", "subject", "body") is True
    assert bot._alert_once("k", "subject", "body") is False
    assert bot._alert_once("other", "subject", "body") is True
    assert len(sent) == 2


def _owned_trade():
    return {"id": 7, "ticker": "AMD", "strategy": "ensemble", "shares": 5,
            "entry_state": "accepted", "client_order_id": "swingv2-entry-ensemble-AMD-1",
            "alpaca_order_id": "entry-1"}


def _gap_client():
    return SimpleNamespace(get_open_position=lambda _t: SimpleNamespace(qty="5"))


def test_protection_gap_reports_unverifiable_protection(monkeypatch):
    monkeypatch.setattr(bot, "_reconcile_entry_fill", lambda *_a: object())
    def unresolved(*_a):
        raise ValueError("protective submission unresolved (not_found)")
    monkeypatch.setattr(bot, "_owned_bracket_orders", unresolved)

    reason = bot._protection_gap(_gap_client(), _owned_trade())

    assert "cannot be verified" in reason


def test_protection_gap_accepts_a_live_linked_stop(monkeypatch):
    monkeypatch.setattr(bot, "_reconcile_entry_fill", lambda *_a: object())
    orders = [SimpleNamespace(type="limit", status="new"),
              SimpleNamespace(type="stop", status="held")]
    monkeypatch.setattr(bot, "_owned_bracket_orders", lambda *_a: orders)

    assert bot._protection_gap(_gap_client(), _owned_trade()) is None


def test_protection_gap_flags_canceled_stop(monkeypatch):
    monkeypatch.setattr(bot, "_reconcile_entry_fill", lambda *_a: object())
    orders = [SimpleNamespace(type="stop", status="canceled")]
    monkeypatch.setattr(bot, "_owned_bracket_orders", lambda *_a: orders)

    assert "no live stop" in bot._protection_gap(_gap_client(), _owned_trade())


def test_protection_gap_ignores_unsettled_entries(monkeypatch):
    def unsettled(*_a):
        raise ValueError("entry quantity is still unsettled")
    monkeypatch.setattr(bot, "_reconcile_entry_fill", unsettled)

    assert bot._protection_gap(_gap_client(), _owned_trade()) is None


@pytest.mark.protection_audit
def test_audit_alerts_an_unprotected_position_once_per_day(monkeypatch):
    sent = []
    monkeypatch.setattr(bot, "send_notification", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(bot, "_get_trading", _gap_client)
    monkeypatch.setattr(bot.db_mod, "get_open_trades", lambda: [_owned_trade()])
    monkeypatch.setattr(bot, "_protection_gap", lambda *_a: "no live stop order is linked to this trade")

    assert len(bot._audit_protection()) == 1
    assert len(bot._audit_protection()) == 1
    assert len(sent) == 1 and "UNPROTECTED" in sent[0][0]


def test_reconciliation_failure_is_returned_and_alerted(monkeypatch):
    sent = []
    monkeypatch.setattr(bot, "send_notification", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(bot, "_get_trading", _gap_client)
    monkeypatch.setattr(bot.db_mod, "get_open_trades", lambda: [_owned_trade()])
    def boom(*_a):
        raise ValueError("broker entry does not match stored ownership")
    monkeypatch.setattr(bot, "_resolve_pending_entry", boom)

    failures = bot._reconcile_and_exit("ensemble", {})

    assert failures and "ownership" in failures[0]
    assert len(sent) == 1 and "reconciliation blocked" in sent[0][0]
