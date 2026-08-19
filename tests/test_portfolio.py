"""Local P&L ledger math — no broker, no database."""
import math

import pytest

import portfolio


def trade(**kw):
    base = {
        "id": 1,
        "ticker": "NVDA",
        "strategy": "ensemble",
        "entry_date": "2026-07-01 12:00:00",
        "entry_price": 100.0,
        "entry_filled_price": None,
        "shares": 10.0,
        "status": "closed",
        "exit_date": "2026-07-05T14:00:00+00:00",
        "pnl_dollars": 50.0,
        "pnl_pct": 0.05,
        "client_order_id": "swingv2-entry-ensemble-NVDA-abc123",
    }
    base.update(kw)
    return base


# ── Cost basis ────────────────────────────────────────────────────────────────

def test_cost_basis_prefers_the_real_broker_fill():
    t = trade(entry_price=100.0, entry_filled_price=101.5, shares=10)
    assert portfolio.cost_basis(t) == pytest.approx(1015.0)


def test_cost_basis_falls_back_to_signal_price_when_unfilled():
    assert portfolio.cost_basis(trade(entry_filled_price=None)) == pytest.approx(1000.0)


def test_zero_share_intent_rows_are_not_trades():
    """Rejected entries are persisted for durability but never traded."""
    assert not portfolio.is_real_trade(trade(shares=0.0, exit_reason="entry_not_filled"))
    assert portfolio.is_real_trade(trade(shares=1.0))


def test_intent_rows_are_excluded_from_counts_and_win_rate():
    snap = portfolio.build_snapshot([
        trade(id=1, pnl_dollars=50.0),
        trade(id=2, shares=0.0, pnl_dollars=0.0),   # never filled
    ])
    assert snap.closed_count == 1
    assert snap.wins == 1
    assert snap.win_rate == pytest.approx(1.0)


# ── Capital base ──────────────────────────────────────────────────────────────

def test_peak_deployed_is_the_max_concurrent_basis():
    trades = [
        trade(id=1, entry_date="2026-07-01 12:00:00",
              exit_date="2026-07-10T12:00:00+00:00"),        # 1000, open 01→10
        trade(id=2, entry_date="2026-07-02 12:00:00",
              exit_date="2026-07-03T12:00:00+00:00"),        # 1000, open 02→03
        trade(id=3, entry_date="2026-07-20 12:00:00",
              exit_date="2026-07-21T12:00:00+00:00"),        # 1000, alone
    ]
    # Two overlap on 02–03; the third never overlaps anything.
    assert portfolio.peak_deployed_capital(trades) == pytest.approx(2000.0)


def test_peak_deployed_counts_still_open_trades():
    trades = [
        trade(id=1, status="open", exit_date=None, pnl_dollars=None),
        trade(id=2, status="open", exit_date=None, pnl_dollars=None),
    ]
    assert portfolio.peak_deployed_capital(trades) == pytest.approx(2000.0)


def test_peak_deployed_charges_both_sides_of_a_same_instant_swap():
    """Freed cash is not reliably available to the next entry (settlement lags)."""
    trades = [
        trade(id=1, entry_date="2026-07-01 12:00:00",
              exit_date="2026-07-05 12:00:00"),
        trade(id=2, entry_date="2026-07-05 12:00:00",
              exit_date="2026-07-09 12:00:00"),
    ]
    assert portfolio.peak_deployed_capital(trades) == pytest.approx(2000.0)


def test_peak_deployed_handles_mixed_naive_and_aware_timestamps():
    """The table mixes both formats; ordering must still be comparable."""
    trades = [
        trade(id=1, entry_date="2026-07-01 12:00:00",
              exit_date="2026-07-02T12:00:00+00:00"),
        trade(id=2, entry_date="2026-07-03T12:00:00+00:00",
              exit_date="2026-07-04 12:00:00"),
    ]
    # Sequential, never overlapping — one position's worth of capital.
    assert portfolio.peak_deployed_capital(trades) == pytest.approx(1000.0)


def test_peak_deployed_ignores_unparseable_entry_dates():
    assert portfolio.peak_deployed_capital([trade(entry_date="not a date")]) == 0.0


# ── Snapshot ──────────────────────────────────────────────────────────────────

def test_realized_and_unrealized_combine_into_equity():
    trades = [
        trade(id=1, pnl_dollars=50.0),                       # closed +50
        trade(id=2, status="open", exit_date=None, pnl_dollars=None,
              ticker="AMD", entry_price=200.0, shares=5.0),  # basis 1000
    ]
    snap = portfolio.build_snapshot(trades, marks={"AMD": 220.0})
    assert snap.realized_pnl == pytest.approx(50.0)
    assert snap.unrealized_pnl == pytest.approx(100.0)       # (220-200) * 5
    assert snap.total_pnl == pytest.approx(150.0)
    assert snap.equity == pytest.approx(snap.starting_capital + 150.0)


def test_return_pct_is_pnl_over_the_capital_base():
    snap = portfolio.build_snapshot([trade(pnl_dollars=100.0)], starting_capital=2000.0)
    assert snap.total_return_pct == pytest.approx(0.05)


def test_a_missing_mark_never_invents_unrealized_pnl():
    """An unknown price must read as a floor, not a guess."""
    trades = [trade(id=1, status="open", exit_date=None, pnl_dollars=None,
                    ticker="ARM", entry_price=250.0, shares=4.0)]
    snap = portfolio.build_snapshot(trades, marks={})
    assert snap.marks_complete is False
    assert snap.unrealized_pnl == pytest.approx(0.0)
    assert snap.open_market_value == pytest.approx(1000.0)   # carried at cost
    assert snap.positions[0].unrealized_pnl is None


@pytest.mark.parametrize("bad_mark", [0.0, -5.0, float("nan"), "n/a", None])
def test_unusable_marks_are_treated_as_missing(bad_mark):
    trades = [trade(id=1, status="open", exit_date=None, pnl_dollars=None)]
    snap = portfolio.build_snapshot(trades, marks={"NVDA": bad_mark})
    assert snap.marks_complete is False
    assert snap.unrealized_pnl == pytest.approx(0.0)


def test_profit_factor_reports_gross_profit_when_there_are_no_losses():
    """Infinity round-trips through neither SQLite nor JSON."""
    snap = portfolio.build_snapshot([trade(id=1, pnl_dollars=75.0)])
    assert math.isfinite(snap.profit_factor)
    assert snap.profit_factor == pytest.approx(75.0)


def test_profit_factor_ratio_with_losses():
    snap = portfolio.build_snapshot([
        trade(id=1, pnl_dollars=100.0),
        trade(id=2, pnl_dollars=-50.0),
    ])
    assert snap.profit_factor == pytest.approx(2.0)


def test_breakeven_trade_counts_as_a_loss_not_a_win():
    snap = portfolio.build_snapshot([trade(pnl_dollars=0.0)])
    assert (snap.wins, snap.losses) == (0, 1)


def test_return_on_deployed_uses_closed_cost_basis():
    snap = portfolio.build_snapshot([
        trade(id=1, pnl_dollars=50.0),      # basis 1000
        trade(id=2, pnl_dollars=-25.0),     # basis 1000
    ])
    assert snap.total_deployed == pytest.approx(2000.0)
    assert snap.return_on_deployed == pytest.approx(25.0 / 2000.0)


def test_broker_status_flows_into_the_snapshot():
    trades = [trade(id=7, status="open", exit_date=None, pnl_dollars=None)]
    snap = portfolio.build_snapshot(
        trades, marks={"NVDA": 105.0}, broker_status={7: "mismatch"},
    )
    assert snap.positions[0].broker_status == "mismatch"
    assert snap.broker_mismatched == 1
    assert snap.broker_confirmed == 0


def test_unlisted_trades_read_as_unverified():
    trades = [trade(id=7, status="open", exit_date=None, pnl_dollars=None)]
    snap = portfolio.build_snapshot(trades, marks={"NVDA": 105.0})
    assert snap.positions[0].broker_status == "unverified"


def test_empty_history_is_all_zeroes_not_a_crash():
    snap = portfolio.build_snapshot([])
    assert snap.equity == 0.0
    assert snap.total_return_pct == 0.0
    assert snap.win_rate == 0.0
    assert snap.positions == ()


def test_snapshot_as_dict_is_json_shaped():
    snap = portfolio.build_snapshot(
        [trade(id=1, status="open", exit_date=None, pnl_dollars=None)],
        marks={"NVDA": 110.0},
    )
    d = snap.as_dict()
    assert isinstance(d["positions"], list)
    assert isinstance(d["positions"][0], dict)
    assert d["positions"][0]["ticker"] == "NVDA"


# ── Rebuilt curve ─────────────────────────────────────────────────────────────

def test_realized_curve_accumulates_by_exit_day():
    curve = portfolio.realized_equity_curve([
        trade(id=1, exit_date="2026-07-05T14:00:00+00:00", pnl_dollars=50.0),
        trade(id=2, exit_date="2026-07-05T16:00:00+00:00", pnl_dollars=25.0),
        trade(id=3, exit_date="2026-07-08T16:00:00+00:00", pnl_dollars=-10.0),
    ], starting_capital=1000.0)
    assert [p["date"] for p in curve] == ["2026-07-05", "2026-07-08"]
    assert curve[0]["realized_pnl"] == pytest.approx(75.0)
    assert curve[0]["closed_trades"] == 2
    assert curve[1]["realized_pnl"] == pytest.approx(65.0)
    assert curve[1]["equity"] == pytest.approx(1065.0)


def test_realized_curve_skips_open_trades():
    curve = portfolio.realized_equity_curve([
        trade(id=1, status="open", exit_date=None, pnl_dollars=None),
    ])
    assert curve == []
