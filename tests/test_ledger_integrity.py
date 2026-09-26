"""Live-readiness 0.1 / 0.4: the ledger refuses double claims, corrupt rows are
quarantined not deleted, and exits forced by other projects stay out of stats."""
from __future__ import annotations

import sqlite3

import pytest

import portfolio
from dashboard import db as db_mod


def _open(ticker="NVDA", coid="swingv2-entry-ensemble-NVDA-a", oid="order-a"):
    return db_mod.save_trade(ticker, "ensemble", "2026-07-01 12:00:00", 100.0, 90.0, 110.0,
                             shares=10, client_order_id=coid, alpaca_order_id=oid)


def _close(trade_id, exit_oid, pnl=50.0, reason="bracket_filled"):
    db_mod.close_trade(trade_id, "2026-07-05T14:00:00+00:00", 105.0, reason, 3, 10,
                       pnl, pnl / 1000, exit_alpaca_order_id=exit_oid)


def test_duplicate_client_order_id_is_rejected():
    _open()
    with pytest.raises(sqlite3.IntegrityError):
        _open(oid="order-b")


def test_duplicate_broker_entry_order_id_is_rejected():
    _open()
    with pytest.raises(sqlite3.IntegrityError):
        _open(coid="swingv2-entry-ensemble-NVDA-b")


def test_one_exit_order_cannot_close_two_trades():
    """21 rows once claimed the same foreign sell (2bd2fcab)."""
    a = _open()
    b = _open(coid="swingv2-entry-ensemble-NVDA-b", oid="order-b")
    _close(a, "sell-1")
    with pytest.raises(sqlite3.IntegrityError):
        _close(b, "sell-1")


def test_unfilled_entry_closed_with_its_own_id_is_allowed():
    a = _open()
    _close(a, "order-a", pnl=0.0, reason="entry_not_filled")


def test_one_exit_fill_cannot_be_recorded_against_two_trades():
    a = _open()
    b = _open(coid="swingv2-entry-ensemble-NVDA-b", oid="order-b")
    db_mod.record_exit_order_progress(a, "sell-1", None, 10, 1050)
    db_mod.record_exit_order_progress(a, "sell-1", None, 10, 1050)  # idempotent
    with pytest.raises(sqlite3.IntegrityError):
        db_mod.record_exit_order_progress(b, "sell-1", None, 10, 1050)


def test_quarantine_moves_rows_with_reason_and_keeps_the_rest():
    bad = _open()
    good = _open(coid="swingv2-entry-ensemble-NVDA-b", oid="order-b")
    db_mod.record_exit_order_progress(bad, "sell-1", None, 10, 1050)
    _close(bad, "sell-1")
    assert db_mod.quarantine_trades([bad], "test reason") == 1
    with db_mod._con() as c:
        assert [r["id"] for r in c.execute("SELECT id FROM trades")] == [good]
        row = c.execute("SELECT * FROM trades_quarantine").fetchone()
        assert (row["id"], row["quarantine_reason"]) == (bad, "test reason")
        assert row["client_order_id"] == "swingv2-entry-ensemble-NVDA-a"
        assert c.execute("SELECT COUNT(*) FROM trade_exit_fills").fetchone()[0] == 0


def test_indexes_that_cannot_build_do_not_stop_startup(tmp_path, monkeypatch):
    """Duplicates already on disk: log it, keep the bot able to start."""
    path = tmp_path / "dupes.db"
    monkeypatch.setattr(db_mod, "_DB", path)
    db_mod.init_db()
    with db_mod._con() as c:
        c.execute("DROP INDEX ux_trades_client_order_id")
        for oid in ("x", "y"):
            c.execute("INSERT INTO trades (ticker, strategy, entry_date, entry_price, "
                      "stop_loss, take_profit, client_order_id, alpaca_order_id, status) "
                      "VALUES ('NVDA','ensemble','2026-07-01',1,1,1,'dup',?,'open')", (oid,))
    db_mod._ensure_tables(force=True)  # must not raise


# ── 0.4: exits forced by another project ─────────────────────────────────────

def _t(id, pnl, reason):
    return {"id": id, "ticker": "NVDA", "strategy": "ensemble", "status": "closed",
            "entry_date": "2026-07-01 12:00:00", "entry_price": 100.0,
            "entry_filled_price": 100.0, "shares": 10.0, "pnl_dollars": pnl,
            "pnl_pct": pnl / 1000, "exit_reason": reason,
            "exit_date": "2026-07-05T14:00:00+00:00"}


def test_snapshot_excludes_external_liquidations_from_stats_not_money():
    trades = [_t(1, 100.0, "bracket_filled"), _t(2, -40.0, "time_stop"),
              _t(3, -500.0, "external_liquidation")]
    snap = portfolio.build_snapshot(trades, {}, starting_capital=10_000)
    assert snap.realized_pnl == pytest.approx(-440.0)      # money is real
    assert (snap.closed_count, snap.wins, snap.losses) == (2, 1, 1)
    assert snap.worst_trade == pytest.approx(-40.0)
    assert snap.profit_factor == pytest.approx(2.5)
    assert (snap.interference_count, snap.interference_pnl) == (1, -500.0)


def test_portfolio_stats_excludes_external_liquidations_from_stats_not_money():
    for i, (pnl, reason) in enumerate([(100.0, "bracket_filled"),
                                       (-500.0, "external_liquidation")]):
        tid = _open(coid=f"c{i}", oid=f"o{i}")
        _close(tid, f"s{i}", pnl=pnl, reason=reason)
    stats = db_mod.portfolio_stats()
    assert (stats["trades"], stats["wins"], stats["win_rate"]) == (1, 1, 100.0)
    assert stats["interference_count"] == 1
    assert stats["total_pnl"] == pytest.approx(-400.0)
