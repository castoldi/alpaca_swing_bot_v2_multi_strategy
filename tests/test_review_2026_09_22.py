"""Regressions for docs/code-review-2026-09-22.md (R01-R03, R05-R07)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import bot
from config import StrategyType
from tests.test_bot_shared_account import (
    _LiquidatedClient,
    _SharedAccountClient,
    _cycle,
    _order,
    _trade,
    liquidation_db,  # noqa: F401  (fixture re-export)
)


class _Unavailable(RuntimeError):
    """A transient broker failure: state unknown, not 'no position'."""
    status_code = 503


class _NoPosition(RuntimeError):
    status_code = 404


def _leg(oid, kind):
    return SimpleNamespace(
        id=oid, client_order_id=f"broker-{oid}", symbol="NVDA",
        side=SimpleNamespace(value="sell"), status=SimpleNamespace(value="new"),
        type=SimpleNamespace(value=kind), filled_qty="0", filled_avg_price=None,
        qty="97", time_in_force=SimpleNamespace(value="gtc"),
    )


class _LiveBracketClient(_LiquidatedClient):
    """Our 97 NVDA are still held under a live bracket; a sibling sold its own
    100 NVDA after our entry; the position lookup itself can fail."""

    def __init__(self, position_error):
        super().__init__([_order("sibling-sell", qty=100, price=205.14,
                                 submitted="2026-07-21T16:28:41+00:00")])
        self.position_error = position_error
        self.submitted = []

    def _entry(self):
        entry = super()._entry()
        entry.legs = [_leg("tp-leg", "limit"), _leg("stop-leg", "stop")]
        return entry

    def get_open_position(self, _ticker):
        raise self.position_error

    def submit_order(self, req):
        self.submitted.append(req)
        raise AssertionError("no order may be submitted")


@pytest.fixture
def reconcile_env(monkeypatch, liquidation_db):
    alerts = []
    monkeypatch.setattr(bot, "_alert_once", lambda key, *_a: alerts.append(key))
    trade = _trade()

    def run(client, trade_row=None):
        monkeypatch.setattr(bot, "_get_trading", lambda: client)
        monkeypatch.setattr(bot.db_mod, "get_open_trades", lambda: [dict(trade_row or trade)])
        return bot._reconcile_and_exit("ensemble")

    return SimpleNamespace(run=run, alerts=alerts, db=liquidation_db)


# ── R01: only a 404 means "no position" ───────────────────────────────────────

def test_transient_position_error_never_finalizes_a_live_trade(reconcile_env):
    failures = reconcile_env.run(_LiveBracketClient(_Unavailable("503 service unavailable")))

    assert reconcile_env.db["closed"] == []
    assert len(failures) == 1 and "503" in failures[0]
    assert reconcile_env.alerts == ["reconcile-38"]


def test_confirmed_missing_position_still_reaches_the_post_mortem(reconcile_env):
    reconcile_env.run(_LiveBracketClient(_NoPosition("position does not exist")))

    assert [c["reason"] for c in reconcile_env.db["closed"]] == ["external_liquidation"]


def test_open_position_helper_reraises_everything_but_404():
    class Client:
        def __init__(self, exc):
            self.exc = exc

        def get_open_position(self, _t):
            raise self.exc

    assert bot._open_position_or_none(Client(_NoPosition("none")), "NVDA") is None
    with pytest.raises(_Unavailable):
        bot._open_position_or_none(Client(_Unavailable("down")), "NVDA")
    with pytest.raises(TimeoutError):
        bot._open_position_or_none(Client(TimeoutError("read timed out")), "NVDA")


def test_exit_defers_when_position_lookup_fails_after_cancel(monkeypatch, liquidation_db):
    client = _LiveBracketClient(_Unavailable("503"))
    monkeypatch.setattr(bot, "_cancel_owned_bracket", lambda *_a: True)
    reconciled = []
    monkeypatch.setattr(bot, "_reconcile_closed", lambda *_a: reconciled.append(1))

    assert bot._execute_exit_intent(client, _trade(), None, "time_stop", "swingv2-exit-x") is False
    assert reconciled == [] and client.submitted == []
    assert liquidation_db["closed"] == []


# ── R02: a working entry inside the grace window is not a failure ─────────────

class _PendingEntryClient(_LiquidatedClient):
    def __init__(self, submitted_at):
        super().__init__([])
        self.submitted_at = submitted_at

    def _entry(self):
        entry = super()._entry()
        entry.status = SimpleNamespace(value="new")
        entry.filled_qty = "0"
        entry.filled_avg_price = None
        entry.submitted_at = self.submitted_at
        return entry

    def get_open_position(self, _t):
        raise _NoPosition("not filled yet")


def _iso(delta):
    return (datetime.now(timezone.utc) - delta).isoformat()


def test_young_pending_entry_is_skipped_quietly(reconcile_env):
    failures = reconcile_env.run(_PendingEntryClient(_iso(timedelta(seconds=30))))

    assert failures == [] and reconcile_env.alerts == []
    assert reconcile_env.db["closed"] == []


def test_stuck_pending_entry_still_alerts(reconcile_env):
    failures = reconcile_env.run(_PendingEntryClient(_iso(timedelta(minutes=10))))

    assert len(failures) == 1 and "unsettled" in failures[0]
    assert reconcile_env.alerts == ["reconcile-38"]


def test_pending_entry_is_still_a_value_error_for_fail_closed_callers():
    with pytest.raises(ValueError):
        bot._reconcile_entry_fill(_PendingEntryClient(_iso(timedelta(0))), _trade())


def test_pending_without_any_timestamp_is_not_assumed_young():
    assert bot._entry_pending_is_young(bot.EntryPending("x", None)) is False


# ── R03: default HTTP timeouts ────────────────────────────────────────────────

class _Session:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append(kwargs)
        return "ok"


def test_default_timeout_applies_unless_caller_sets_one():
    from http_timeouts import DEFAULT_TIMEOUT, apply_default_timeout

    client = SimpleNamespace(_session=_Session())
    calls = client._session.calls
    apply_default_timeout(client)
    apply_default_timeout(client)  # idempotent: no double wrapping

    client._session.request("GET", "u")
    client._session.request("GET", "u", timeout=2)
    assert calls == [{"timeout": DEFAULT_TIMEOUT}, {"timeout": 2}]


def test_real_alpaca_clients_get_the_timeout(monkeypatch):
    from http_timeouts import DEFAULT_TIMEOUT
    import data_feed

    monkeypatch.setattr(bot, "_trading_client", None)
    monkeypatch.setattr(data_feed, "_client", None)
    assert bot._get_trading()._session._swing_default_timeout == DEFAULT_TIMEOUT
    assert data_feed._get_client()._session._swing_default_timeout == DEFAULT_TIMEOUT


# ── R05: SQLite connection handling ───────────────────────────────────────────

def test_every_connection_is_closed(monkeypatch):
    from dashboard import db

    opened = []
    real_connect = sqlite3.connect

    def tracking(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(db.sqlite3, "connect", tracking)
    db.set_signal_cursor("ensemble", "NVDA", "2026-09-22T12:00:00")
    assert db.get_signal_cursor("ensemble", "NVDA") == "2026-09-22T12:00:00"
    assert opened
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


def test_connections_wait_for_locks_instead_of_failing_fast():
    from dashboard import db

    with db._con() as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 30_000


def test_failed_transaction_rolls_back_and_closes():
    from dashboard import db

    with pytest.raises(RuntimeError):
        with db._con() as conn:
            conn.execute("INSERT INTO signal_cursor VALUES ('s','T','x','y')")
            raise RuntimeError("boom")
    assert db.get_signal_cursor("s", "T") is None


def test_schema_ddl_runs_once_per_database(monkeypatch):
    from dashboard import db

    calls = []
    monkeypatch.setattr(db, "_migrate", lambda c: calls.append(1))
    db.get_open_trades()
    db.get_open_trades()
    assert calls == []          # conftest already initialised this path
    db.init_db()
    assert calls == [1]         # explicit init still re-runs migrations


# ── R06: signals on held tickers are neither counted nor stored ───────────────

def test_held_ticker_signal_is_not_counted(monkeypatch):
    placed = _cycle(
        monkeypatch,
        _SharedAccountClient(our_symbols=["NVDA"]),
        ["NVDA"],
        open_trade={"id": 14, "ticker": "NVDA", "strategy": "ensemble"},
    )
    logged, finished = [], []
    monkeypatch.setattr(bot.bot_hooks, "log_signal", lambda *a: logged.append(a))
    monkeypatch.setattr(bot.db_mod, "finish_bot_run", lambda *a: finished.append(a))

    bot.run_once(StrategyType.ENSEMBLE)

    assert placed == [] and logged == []
    assert finished[0][1] == 0          # trades_found


def test_actionable_signal_is_still_counted(monkeypatch):
    placed = _cycle(monkeypatch, _SharedAccountClient(), ["NVDA"], open_trade=None)
    logged, finished = [], []
    monkeypatch.setattr(bot.bot_hooks, "log_signal", lambda *a: logged.append(a))
    monkeypatch.setattr(bot.db_mod, "finish_bot_run", lambda *a: finished.append(a))

    bot.run_once(StrategyType.ENSEMBLE)

    assert len(placed) == 1 and len(logged) == 1
    assert finished[0][1] == 1


# ── R07: passes land just after each modelled fill time ───────────────────────

import pandas as pd  # noqa: E402

THIRTY = timedelta(minutes=30)


@pytest.mark.parametrize("now, timeframe, expected", [
    ("2026-09-22 15:50", "4h", "2026-09-22 16:00"),   # midday bucket, in session
    ("2026-09-22 20:10", "4h", "2026-09-23 13:30"),   # after close -> next open
    ("2026-09-26 12:00", "4h", "2026-09-28 13:30"),   # weekend -> Monday open
    ("2026-12-01 14:20", "4h", "2026-12-01 14:30"),   # EST open is 14:30 UTC
    ("2026-11-27 17:50", "4h", "2026-11-30 14:30"),   # early close 18:00 UTC
    ("2026-09-22 20:30", "1d", "2026-09-23 13:30"),   # daily bar -> next open
])
def test_next_signal_fill_after(now, timeframe, expected):
    assert bot._next_signal_fill_after(pd.Timestamp(now), timeframe) == pd.Timestamp(expected)


@pytest.mark.parametrize("now, expected_seconds", [
    ("2026-09-22 15:50", 11 * 60 + 30),   # wake at 16:01:30, not 16:20
    ("2026-09-23 13:10", 21 * 60 + 30),   # wake at the open + 90 s
    ("2026-09-22 20:10", 30 * 60),        # next fill is tomorrow: one interval
    ("2026-09-22 16:05", 30 * 60),        # fill just passed: one interval
])
def test_seconds_until_next_pass(now, expected_seconds):
    assert bot._seconds_until_next_pass(pd.Timestamp(now), THIRTY, "4h") == expected_seconds


@pytest.mark.real_signal_window
def test_scheduled_wake_is_inside_the_real_fill_window():
    wake = pd.Timestamp("2026-09-22 16:00") + bot.FILL_WAKE_DELAY
    assert bot._signal_is_actionable(pd.Timestamp("2026-09-22 12:00"), "4h", now=wake)


def test_scheduler_falls_back_to_one_interval(monkeypatch):
    def boom(*_a):
        raise RuntimeError("calendar unavailable")

    monkeypatch.setattr(bot, "_next_signal_fill_after", boom)
    assert bot._seconds_until_next_pass(pd.Timestamp("2026-09-22 15:50"), THIRTY, "4h") == 1800


# ── R07: execution quality against the modelled fill ──────────────────────────

@pytest.mark.execution_quality
def test_execution_quality_backfills_and_prices_the_modelled_minute(monkeypatch):
    from dashboard import db

    trade_id = db.save_trade("AMZN", "ensemble", "2026-09-21 16:00:00", 258.405,
                             235.15, 268.74, shares=90)
    db.set_entry_fill(trade_id, 255.78, 90, "2026-09-22T13:33:44+00:00")
    requested = []

    def minute_bars(ticker, start, end, timeframe):
        requested.append((ticker, start, end, timeframe))
        # No IEX trade in 13:30; the first printed minute is 13:31.
        return pd.DataFrame({"open": [255.10, 255.40]},
                            index=pd.to_datetime(["2026-09-22 13:31", "2026-09-22 13:32"]))

    monkeypatch.setattr(bot.data_feed, "fetch_bars", minute_bars)
    assert bot._record_execution_quality(now=pd.Timestamp("2026-09-22 14:00")) == 1

    row = db.get_execution_quality()[0]
    assert row["modelled_fill_at"] == "2026-09-22T13:30:00"
    assert row["model_fill_price"] == pytest.approx(255.10)
    assert requested[0][3] == "1min"

    # Priced once: a later cycle does not fetch again.
    assert bot._record_execution_quality(now=pd.Timestamp("2026-09-22 14:30")) == 0
    assert len(requested) == 1


@pytest.mark.execution_quality
def test_execution_quality_waits_for_the_search_window(monkeypatch):
    from dashboard import db

    trade_id = db.save_trade("META", "ensemble", "2026-09-22 12:00:00", 753.465, 685.65,
                             794.61, shares=31, modelled_fill_at="2026-09-22T16:00:00")
    db.set_entry_fill(trade_id, 748.55, 31, "2026-09-22T16:04:06+00:00")
    monkeypatch.setattr(bot.data_feed, "fetch_bars",
                        lambda *_a, **_k: pytest.fail("fetched before the window printed"))
    assert bot._record_execution_quality(now=pd.Timestamp("2026-09-22 16:05")) == 0


@pytest.mark.execution_quality
def test_execution_quality_never_raises(monkeypatch):
    monkeypatch.setattr(bot.db_mod, "get_trades_missing_modelled_fill",
                        lambda: (_ for _ in ()).throw(RuntimeError("db locked")))
    assert bot._record_execution_quality() == 0


def test_execution_quality_summary():
    from dashboard.server import execution_quality

    summary = execution_quality([{
        "id": 77, "ticker": "AMZN", "strategy": "ensemble", "entry_price": 258.405,
        "modelled_fill_at": "2026-09-22T13:30:00", "model_fill_price": 255.10,
        "entry_filled_at": "2026-09-22T13:33:44+00:00", "entry_filled_price": 255.78,
    }])

    row = summary["trades"][0]
    assert row["delay_sec"] == pytest.approx(224.0)
    assert row["slip_vs_model_pct"] == pytest.approx(0.267, abs=1e-3)
    assert row["slip_vs_signal_pct"] == pytest.approx(-1.016, abs=1e-3)
    assert summary["median_delay_sec"] == pytest.approx(224.0)
