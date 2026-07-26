"""§475(f), substantially-identical groups, crypto, hard block, brackets, lots."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import tax

UTC = timezone.utc


def _trade(tid, ticker, entry, exit_, shares=10, entry_px=100.0, exit_px=100.0,
           status="closed"):
    return {
        "id": tid, "ticker": ticker, "status": status,
        "entry_date": entry, "exit_date": exit_,
        "entry_price": entry_px, "exit_price": exit_px, "shares": shares,
        "pnl_dollars": (exit_px - entry_px) * shares,
    }


def _d(s):
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


# ── §475(f) mark-to-market election ───────────────────────────────────────────

def test_475f_disables_wash_sales_entirely():
    trades = [
        _trade(1, "NVDA", "2026-03-01", "2026-03-05", exit_px=90.0),
        _trade(2, "NVDA", "2026-03-08", "2026-03-20", exit_px=95.0),
    ]
    recs = {r.trade_id: r for r in tax.compute_tax_records(trades, mtm_475f=True)}
    assert recs[1].is_wash_sale is False
    assert recs[1].deductible_pnl == pytest.approx(-100.0)


def test_475f_marks_everything_ordinary():
    rec = tax.compute_tax_records(
        [_trade(1, "NVDA", "2024-01-01", "2025-06-01", exit_px=110.0)],
        mtm_475f=True,
    )[0]
    assert rec.term == "ordinary"      # would be "long" without the election


def test_475f_removes_the_capital_loss_limit():
    trades = [_trade(1, "NVDA", "2026-01-01", "2026-01-10", shares=1000, exit_px=95.0)]
    recs = tax.compute_tax_records(trades, mtm_475f=True)
    s = tax.summarize_year(recs, 2026, 0.24, 0.15, mtm_475f=True)
    assert s["net_capital_gain"] == pytest.approx(-5000.0)
    assert s["loss_deductible_this_year"] == pytest.approx(5000.0)
    assert s["loss_carryforward"] == 0.0


def test_475f_suppresses_the_entry_guard():
    trades = [_trade(1, "NVDA", "2026-12-01", "2026-12-10", exit_px=90.0)]
    assert tax.year_end_entry_block(
        "NVDA", _d("2026-12-15"), trades, mtm_475f=True
    ) is None


def test_475f_defaults_off():
    rec = tax.compute_tax_records(
        [_trade(1, "NVDA", "2026-01-01", "2026-01-10", exit_px=110.0)]
    )[0]
    assert rec.term == "short"


# ── Substantially identical groups ────────────────────────────────────────────

def test_exact_symbol_matching_is_the_default():
    assert tax.identical_symbols("QQQ") == frozenset({"QQQ"})
    assert tax.identical_symbols("QQQ", []) == frozenset({"QQQ"})


def test_configured_group_makes_symbols_identical():
    groups = [("QQQ", "TQQQ")]
    assert tax.identical_symbols("QQQ", groups) == frozenset({"QQQ", "TQQQ"})
    assert tax.identical_symbols("TQQQ", groups) == frozenset({"QQQ", "TQQQ"})
    assert tax.identical_symbols("SPY", groups) == frozenset({"SPY"})


def test_group_causes_a_cross_symbol_wash_sale():
    trades = [
        _trade(1, "QQQ", "2026-03-01", "2026-03-05", exit_px=90.0),
        _trade(2, "TQQQ", "2026-03-08", "2026-03-20", exit_px=105.0),
    ]
    clean = {r.trade_id: r for r in tax.compute_tax_records(trades)}
    assert clean[1].is_wash_sale is False        # default: different symbols

    grouped = {r.trade_id: r for r in
               tax.compute_tax_records(trades, identical_groups=[("QQQ", "TQQQ")])}
    assert grouped[1].is_wash_sale is True
    assert grouped[1].replacement_trade_id == 2


# ── Crypto: tracked, but §1091 not applied ────────────────────────────────────

def test_crypto_losses_are_tracked_but_never_washed():
    trades = [
        _trade(1, "BTCUSD", "2026-03-01", "2026-03-05", exit_px=90.0),
        _trade(2, "BTCUSD", "2026-03-08", "2026-03-20", exit_px=95.0),
    ]
    recs = {r.trade_id: r
            for r in tax.compute_tax_records(trades, crypto_symbols=["BTCUSD"])}
    assert recs[1].is_wash_sale is False
    assert recs[1].realized_pnl == pytest.approx(-100.0)
    assert recs[1].deductible_pnl == pytest.approx(-100.0)   # fully deductible


def test_securities_still_wash_when_crypto_is_configured():
    trades = [
        _trade(1, "NVDA", "2026-03-01", "2026-03-05", exit_px=90.0),
        _trade(2, "NVDA", "2026-03-08", "2026-03-20", exit_px=95.0),
    ]
    recs = {r.trade_id: r
            for r in tax.compute_tax_records(trades, crypto_symbols=["BTCUSD"])}
    assert recs[1].is_wash_sale is True


def test_wash_sale_applies_helper():
    assert tax.wash_sale_applies("NVDA") is True
    assert tax.wash_sale_applies("NVDA", mtm_475f=True) is False
    assert tax.wash_sale_applies("BTCUSD", crypto_symbols=["BTCUSD"]) is False


# ── Conservative hard block ───────────────────────────────────────────────────

@pytest.mark.parametrize("when,blocked", [
    ("2026-12-10", False),   # before the flat window opens
    ("2026-12-16", True),    # inside
    ("2026-12-31", True),    # the boundary itself
    ("2027-01-10", True),    # still inside
    ("2027-01-20", False),   # window closed
])
def test_hard_block_is_a_flat_window_spanning_year_end(when, blocked):
    reason = tax.year_end_entry_block(
        "NVDA", _d(when), [], hard_block=True, hard_block_days=31
    )
    assert (reason is not None) is blocked


def test_hard_block_needs_no_prior_loss():
    """Unlike the surgical guard, it refuses entries unconditionally."""
    reason = tax.year_end_entry_block("NVDA", _d("2026-12-20"), [], hard_block=True)
    assert reason is not None and "hard block" in reason


def test_hard_block_off_by_default():
    assert tax.year_end_entry_block("NVDA", _d("2026-12-20"), []) is None


# ── Brackets, NIIT, estimated payments ────────────────────────────────────────

def test_progressive_brackets_stack_on_other_income():
    low = tax.bracket_liability(10_000, 0, other_income=0, apply_niit=False)
    high = tax.bracket_liability(10_000, 0, other_income=300_000, apply_niit=False)
    assert high["ordinary_tax"] > low["ordinary_tax"]


def test_long_term_gains_can_be_taxed_at_zero():
    out = tax.bracket_liability(0, 20_000, other_income=0, apply_niit=False)
    assert out["ltcg_tax"] == 0.0


def test_niit_only_applies_above_the_threshold():
    below = tax.bracket_liability(50_000, 0, other_income=100_000, apply_niit=True)
    above = tax.bracket_liability(50_000, 0, other_income=400_000, apply_niit=True)
    assert below["niit"] == 0.0
    assert above["niit"] == pytest.approx(50_000 * tax.NIIT_RATE)


def test_niit_is_capped_by_the_amount_over_the_threshold():
    out = tax.bracket_liability(5_000, 0, other_income=199_000, apply_niit=True)
    # MAGI 204,000 -> only 4,000 over the 200,000 threshold, less than the gain
    assert out["niit"] == pytest.approx(4_000 * tax.NIIT_RATE)


def test_married_threshold_is_higher():
    single = tax.bracket_liability(
        50_000, 0, other_income=200_000, filing_status="single")
    joint = tax.bracket_liability(
        50_000, 0, other_income=200_000, filing_status="married_joint")
    assert joint["niit"] < single["niit"]


def test_bracket_mode_reports_a_loss_without_tax():
    out = tax.bracket_liability(-10_000, 0)
    assert out["total_tax"] == 0.0
    assert out["loss_deductible_this_year"] == pytest.approx(3000.0)
    assert out["loss_carryforward"] == pytest.approx(7000.0)


def test_summary_can_switch_to_bracket_mode():
    trades = [_trade(1, "NVDA", "2026-01-01", "2026-01-10", shares=100, exit_px=200.0)]
    recs = tax.compute_tax_records(trades)
    flat = tax.summarize_year(recs, 2026, 0.24, 0.15)
    prog = tax.summarize_year(recs, 2026, 0.24, 0.15, use_brackets=True,
                              other_income=0.0, apply_niit=True)
    assert flat["uses_brackets"] is False and prog["uses_brackets"] is True
    # $10k of gain sits in the low brackets, so progressive beats a flat 24%
    assert prog["estimated_tax"] < flat["estimated_tax"]


def test_estimated_payment_schedule_splits_across_four_dates():
    sched = tax.estimated_payment_schedule(4000.0, 2026)
    assert [p["quarter"] for p in sched] == ["Q1", "Q2", "Q3", "Q4"]
    assert all(p["amount"] == pytest.approx(1000.0) for p in sched)
    assert sched[-1]["due"].startswith("2027-01")   # Q4 falls in the next year


def test_no_schedule_when_nothing_is_owed():
    assert tax.estimated_payment_schedule(0.0, 2026) == []


# ── Lot ledger ────────────────────────────────────────────────────────────────

def test_each_entry_opens_a_lot_and_each_exit_consumes_it():
    trades = [_trade(1, "NVDA", "2026-03-01", "2026-03-05", shares=10, exit_px=110.0)]
    lots, disposals = tax.build_lot_ledger(trades)
    assert len(lots) == 1 and lots[0].closed is True
    assert disposals[0].cost_basis == pytest.approx(1000.0)
    assert disposals[0].realized_pnl == pytest.approx(100.0)


def test_fifo_relieves_the_oldest_lot_first():
    trades = [
        _trade(1, "NVDA", "2026-01-01", "2026-04-01", shares=10,
               entry_px=100.0, exit_px=120.0),
        _trade(2, "NVDA", "2026-02-01", "2026-05-01", shares=10,
               entry_px=200.0, exit_px=120.0),
    ]
    _, disposals = tax.build_lot_ledger(trades, "fifo")
    first = [d for d in disposals if d.trade_id == 1][0]
    assert first.lot_ids == ("L1",)                  # oldest lot, $100 basis
    assert first.cost_basis == pytest.approx(1000.0)


def test_lifo_relieves_the_newest_lot_first():
    trades = [
        _trade(1, "NVDA", "2026-01-01", "2026-04-01", shares=10,
               entry_px=100.0, exit_px=120.0),
        _trade(2, "NVDA", "2026-02-01", "2026-05-01", shares=10,
               entry_px=200.0, exit_px=120.0),
    ]
    _, disposals = tax.build_lot_ledger(trades, "lifo")
    first = [d for d in disposals if d.trade_id == 1][0]
    assert first.lot_ids == ("L2",)                  # newest lot, $200 basis
    assert first.cost_basis == pytest.approx(2000.0)


def test_specific_identification_overrides_the_default_order():
    trades = [
        _trade(1, "NVDA", "2026-01-01", "2026-04-01", shares=10,
               entry_px=100.0, exit_px=120.0),
        _trade(2, "NVDA", "2026-02-01", "2026-05-01", shares=10,
               entry_px=200.0, exit_px=120.0),
    ]
    _, disposals = tax.build_lot_ledger(trades, "specific", specific_lots={1: ["L2"]})
    first = [d for d in disposals if d.trade_id == 1][0]
    assert first.lot_ids == ("L2",)


def test_a_sale_can_span_two_lots():
    trades = [
        _trade(1, "NVDA", "2026-01-01", None, shares=6, entry_px=100.0, status="open"),
        _trade(2, "NVDA", "2026-01-02", "2026-03-01", shares=10,
               entry_px=100.0, exit_px=110.0),
    ]
    _, disposals = tax.build_lot_ledger(trades, "fifo")
    sale = disposals[0]
    assert len(sale.lot_ids) == 2                    # drew on both open lots
    assert sale.quantity == pytest.approx(10.0)


def test_wash_basis_adjustment_raises_the_replacement_lot_cost():
    trades = [
        _trade(1, "NVDA", "2026-03-01", "2026-03-05", shares=10, exit_px=90.0),
        _trade(2, "NVDA", "2026-03-08", None, shares=10,
               entry_px=90.0, status="open"),
    ]
    records = tax.compute_tax_records(trades)
    lots, _ = tax.build_lot_ledger(trades)
    lots = tax.apply_wash_basis_adjustments(lots, records)
    replacement = [l for l in lots if l.trade_id == 2][0]
    assert replacement.basis_adjustment == pytest.approx(100.0)
    assert replacement.adjusted_cost_per_share == pytest.approx(100.0)  # 90 + 10
