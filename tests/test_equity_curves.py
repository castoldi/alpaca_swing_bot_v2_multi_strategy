"""Growth-of-$1 chaining across annual resets, and mark-to-market rebuilds."""
import pytest

import portfolio
from dashboard import db as db_mod


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "_DB", tmp_path / "curves.db")
    db_mod.init_db()
    return db_mod


# ── Chaining ──────────────────────────────────────────────────────────────────

def test_growth_chains_across_the_annual_reset(db):
    """Backtests restart at $1000 each January; the curve must still compound."""
    db.save_equity_curve("ensemble", 2024, [
        ("2024-01-01T00:00:00", 1000.0),
        ("2024-12-31T20:00:00", 1200.0),      # +20%
    ], 1000.0)
    db.save_equity_curve("ensemble", 2025, [
        ("2025-01-01T00:00:00", 1000.0),
        ("2025-12-31T20:00:00", 1100.0),      # +10%
    ], 1000.0)

    pts = db.get_equity_curves()["ensemble"]
    assert pts[0]["growth"] == pytest.approx(1.0)
    assert pts[1]["growth"] == pytest.approx(1.2)
    assert pts[2]["growth"] == pytest.approx(1.2)     # 2025 opens where 2024 closed
    assert pts[3]["growth"] == pytest.approx(1.32)    # 1.2 * 1.1


def test_a_losing_year_compounds_downward(db):
    db.save_equity_curve("regime", 2024, [("2024-12-31T20:00:00", 800.0)], 1000.0)
    db.save_equity_curve("regime", 2025, [("2025-12-31T20:00:00", 1500.0)], 1000.0)
    pts = db.get_equity_curves()["regime"]
    assert pts[-1]["growth"] == pytest.approx(0.8 * 1.5)


def test_from_year_rebases_to_one(db):
    """Picking a later start year restarts the race at $1, ignoring earlier years."""
    for year, end in ((2024, 2000.0), (2025, 1100.0), (2026, 1200.0)):
        db.save_equity_curve("ensemble", year, [
            (f"{year}-01-01T00:00:00", 1000.0),
            (f"{year}-12-31T20:00:00", end),
        ], 1000.0)

    from_2025 = db.get_equity_curves(2025)["ensemble"]
    assert from_2025[0]["growth"] == pytest.approx(1.0)
    assert from_2025[-1]["growth"] == pytest.approx(1.1 * 1.2)
    assert all(p["year"] >= 2025 for p in from_2025)


def test_strategies_are_chained_independently(db):
    db.save_equity_curve("a", 2024, [("2024-12-31T20:00:00", 2000.0)], 1000.0)
    db.save_equity_curve("b", 2024, [("2024-12-31T20:00:00", 500.0)], 1000.0)
    db.save_equity_curve("a", 2025, [("2025-12-31T20:00:00", 1000.0)], 1000.0)
    db.save_equity_curve("b", 2025, [("2025-12-31T20:00:00", 1000.0)], 1000.0)
    curves = db.get_equity_curves()
    assert curves["a"][-1]["growth"] == pytest.approx(2.0)
    assert curves["b"][-1]["growth"] == pytest.approx(0.5)


def test_rerunning_a_year_replaces_it_rather_than_doubling(db):
    db.save_equity_curve("ensemble", 2024, [("2024-06-01T20:00:00", 1100.0)], 1000.0)
    db.save_equity_curve("ensemble", 2024, [("2024-06-01T20:00:00", 1300.0)], 1000.0)
    pts = db.get_equity_curves()["ensemble"]
    assert len(pts) == 1
    assert pts[0]["growth"] == pytest.approx(1.3)


def test_years_listing_is_sorted(db):
    db.save_equity_curve("x", 2026, [("2026-01-01T00:00:00", 1000.0)], 1000.0)
    db.save_equity_curve("x", 2016, [("2016-01-01T00:00:00", 1000.0)], 1000.0)
    assert db.get_equity_curve_years() == [2016, 2026]


def test_zero_initial_equity_is_rejected(db):
    """A zero base would make every growth factor infinite."""
    with pytest.raises(ValueError):
        db.save_equity_curve("x", 2024, [("2024-01-01T00:00:00", 1000.0)], 0.0)


def test_no_curves_is_an_empty_dict(db):
    assert db.get_equity_curves() == {}


# ── Mark-to-market daily curve ────────────────────────────────────────────────

def trade(**kw):
    base = {
        "id": 1, "ticker": "NVDA", "strategy": "ensemble",
        "entry_date": "2026-07-01 12:00:00", "entry_price": 100.0,
        "entry_filled_price": None, "shares": 10.0, "status": "open",
        "exit_date": None, "pnl_dollars": None, "pnl_pct": None,
        "client_order_id": "swingv2-entry-x",
    }
    base.update(kw)
    return base


CLOSES = {"NVDA": {"2026-07-01": 100.0, "2026-07-02": 110.0, "2026-07-03": 95.0}}


def test_open_position_is_marked_each_day():
    """The whole point: a realized-only curve is flat here, hiding the swing."""
    curve = portfolio.daily_equity_curve([trade()], CLOSES, starting_capital=1000.0)
    assert [p["unrealized_pnl"] for p in curve] == [0.0, 100.0, -50.0]
    assert [p["equity"] for p in curve] == [1000.0, 1100.0, 950.0]
    assert all(p["open_positions"] == 1 for p in curve)


def test_realized_replaces_unrealized_after_the_exit_day():
    curve = portfolio.daily_equity_curve([
        trade(status="closed", exit_date="2026-07-02T20:00:00+00:00", pnl_dollars=100.0),
    ], CLOSES, starting_capital=1000.0)
    by_day = {p["date"]: p for p in curve}
    assert by_day["2026-07-01"]["unrealized_pnl"] == pytest.approx(0.0)
    assert by_day["2026-07-02"]["realized_pnl"] == pytest.approx(100.0)
    assert by_day["2026-07-02"]["unrealized_pnl"] == pytest.approx(0.0)
    assert by_day["2026-07-03"]["equity"] == pytest.approx(1100.0)
    assert by_day["2026-07-03"]["open_positions"] == 0


def test_a_trade_is_absent_before_its_entry_day():
    curve = portfolio.daily_equity_curve(
        [trade(entry_date="2026-07-03 12:00:00")], CLOSES, starting_capital=1000.0,
    )
    by_day = {p["date"]: p for p in curve}
    assert by_day["2026-07-01"]["open_positions"] == 0
    assert by_day["2026-07-01"]["equity"] == pytest.approx(1000.0)
    assert by_day["2026-07-03"]["open_positions"] == 1


def test_a_missing_close_marks_at_cost_and_flags_partial():
    """Never invent a price: mark flat and say so."""
    closes = {"NVDA": {"2026-07-01": 100.0, "2026-07-02": 110.0}}
    curve = portfolio.daily_equity_curve(
        [trade(ticker="AMD", entry_price=50.0, shares=4.0)], closes,
        starting_capital=1000.0,
    )
    assert all(p["partial"] for p in curve)
    assert all(p["unrealized_pnl"] == 0.0 for p in curve)


def test_partial_is_false_when_every_position_is_marked():
    curve = portfolio.daily_equity_curve([trade()], CLOSES, starting_capital=1000.0)
    assert not any(p["partial"] for p in curve)


def test_no_closes_yields_no_curve():
    assert portfolio.daily_equity_curve([trade()], {}) == []


def test_intent_rows_never_enter_the_curve():
    curve = portfolio.daily_equity_curve(
        [trade(shares=0.0, status="closed", exit_date="2026-07-02T20:00:00+00:00",
               pnl_dollars=0.0)],
        CLOSES, starting_capital=1000.0,
    )
    assert all(p["open_positions"] == 0 for p in curve)
    assert all(p["equity"] == 1000.0 for p in curve)


def test_rebuilt_rows_keep_the_unrealized_leg(db):
    """The stored row must round-trip equity = base + realized + unrealized."""
    db.replace_rebuilt_balance_history([
        {"date": "2026-07-02", "realized_pnl": 50.0, "unrealized_pnl": -20.0,
         "equity": 1030.0, "open_positions": 2, "partial": False},
    ])
    (row,) = db.get_balance_history()
    assert row["realized_pnl"] == pytest.approx(50.0)
    assert row["unrealized_pnl"] == pytest.approx(-20.0)
    assert row["starting_capital"] == pytest.approx(1000.0)
    assert row["open_positions"] == 2
    assert row["marks_complete"] == 1


def test_a_partial_day_is_stored_as_incomplete_marks(db):
    db.replace_rebuilt_balance_history([
        {"date": "2026-07-02", "realized_pnl": 0.0, "unrealized_pnl": 0.0,
         "equity": 1000.0, "open_positions": 1, "partial": True},
    ])
    assert db.get_balance_history()[0]["marks_complete"] == 0
