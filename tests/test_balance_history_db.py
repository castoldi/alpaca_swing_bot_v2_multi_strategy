"""Persistence for the bot's own equity curve and broker verdicts."""
import pytest

from dashboard import db as db_mod


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Point the db module at a throwaway file, freshly migrated."""
    monkeypatch.setattr(db_mod, "_DB", tmp_path / "test.db")
    db_mod.init_db()
    return db_mod


def snapshot(**kw):
    base = {
        "ts": "2026-08-19T12:00:00+00:00",
        "strategy": "ensemble",
        "starting_capital": 110000.0,
        "realized_pnl": 4518.30,
        "unrealized_pnl": 250.0,
        "equity": 114768.30,
        "open_count": 5,
        "open_cost_basis": 89095.26,
        "open_market_value": 89345.26,
        "closed_count": 50,
        "wins": 36,
        "losses": 14,
        "marks_complete": True,
        "broker_confirmed": 5,
        "broker_mismatched": 0,
    }
    base.update(kw)
    return base


def test_snapshot_round_trips(db):
    db.save_balance_snapshot(snapshot(), broker_equity=999_000.0)
    latest = db.get_latest_balance()
    assert latest["equity"] == pytest.approx(114768.30)
    assert latest["realized_pnl"] == pytest.approx(4518.30)
    assert latest["wins"] == 36
    assert latest["broker_equity"] == pytest.approx(999_000.0)
    assert latest["source"] == "bot_run"


def test_marks_complete_stores_as_an_integer_flag(db):
    db.save_balance_snapshot(snapshot(marks_complete=False))
    assert db.get_latest_balance()["marks_complete"] == 0


def test_history_comes_back_oldest_first(db):
    for ts in ("2026-08-19T14:00:00+00:00",
               "2026-08-19T12:00:00+00:00",
               "2026-08-19T13:00:00+00:00"):
        db.save_balance_snapshot(snapshot(ts=ts))
    rows = db.get_balance_history()
    assert [r["ts"] for r in rows] == sorted(r["ts"] for r in rows)


def test_history_limit_keeps_the_newest_points(db):
    for hour in range(10, 20):
        db.save_balance_snapshot(snapshot(ts=f"2026-08-19T{hour}:00:00+00:00"))
    rows = db.get_balance_history(limit=3)
    assert [r["ts"][11:13] for r in rows] == ["17", "18", "19"]


def test_daily_history_keeps_one_point_per_day(db):
    db.save_balance_snapshot(snapshot(ts="2026-08-18T12:00:00+00:00", equity=100.0))
    db.save_balance_snapshot(snapshot(ts="2026-08-18T20:00:00+00:00", equity=200.0))
    db.save_balance_snapshot(snapshot(ts="2026-08-19T20:00:00+00:00", equity=300.0))
    rows = db.get_daily_balance_history()
    assert len(rows) == 2
    assert [r["equity"] for r in rows] == [200.0, 300.0]  # last of each day


def test_latest_balance_is_none_before_any_snapshot(db):
    assert db.get_latest_balance() is None


def test_rebuild_never_destroys_live_snapshots(db):
    """A backfill may only replace rows it wrote itself."""
    db.save_balance_snapshot(snapshot(ts="2026-08-19T20:00:00+00:00"), source="bot_run")
    db.replace_rebuilt_balance_history([
        {"date": "2026-07-01", "realized_pnl": 10.0, "equity": 1010.0, "closed_trades": 1},
    ])
    sources = {r["source"] for r in db.get_balance_history()}
    assert sources == {"bot_run", "rebuilt"}


def test_rebuild_replaces_only_previous_rebuilt_rows(db):
    points = [{"date": "2026-07-01", "realized_pnl": 10.0, "equity": 1010.0, "closed_trades": 1}]
    db.replace_rebuilt_balance_history(points)
    db.replace_rebuilt_balance_history(points)
    rebuilt = [r for r in db.get_balance_history() if r["source"] == "rebuilt"]
    assert len(rebuilt) == 1


# ── Broker verdicts on trades ─────────────────────────────────────────────────

def test_broker_verdict_is_recorded_on_an_open_trade(db):
    tid = db.save_trade("NVDA", "ensemble", "2026-08-17 12:00:00", 227.7, 205.0,
                        240.0, shares=90, client_order_id="swingv2-entry-x")
    db.set_broker_sync(tid, "confirmed", 90.0)
    (row,) = [t for t in db.get_open_trades() if t["id"] == tid]
    assert row["broker_status"] == "confirmed"
    assert row["broker_shares"] == pytest.approx(90.0)
    assert row["broker_checked_at"]


def test_broker_verdict_does_not_rewrite_a_closed_trade(db):
    """Once closed, the verdict is history — a later sweep must not overwrite it."""
    tid = db.save_trade("NVDA", "ensemble", "2026-08-17 12:00:00", 227.7, 205.0,
                        240.0, shares=90, client_order_id="swingv2-entry-x")
    db.set_broker_sync(tid, "confirmed", 90.0)
    db.close_trade(tid, "2026-08-18T12:00:00+00:00", 230.0, "time_stop", 1, 90, 207.0, 0.01)
    db.set_broker_sync(tid, "missing", 0.0)
    (row,) = [t for t in db.get_closed_trades() if t["id"] == tid]
    assert row["broker_status"] == "confirmed"


def test_ledger_query_is_not_truncated_by_a_limit(db):
    """Lifetime totals must see every trade, not the most recent page of them."""
    for i in range(250):
        db.save_trade("NVDA", "ensemble", f"2026-01-01 12:00:00", 100.0, 90.0,
                      110.0, shares=1)
    assert len(db.get_trades_for_ledger()) == 250
    assert len(db.get_all_trades()) == 200      # the paged view still pages
