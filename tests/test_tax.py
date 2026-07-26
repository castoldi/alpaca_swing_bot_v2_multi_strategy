"""Wash-sale, term classification, forecast, and year-end guard rules."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


# ── Date parsing ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "2026-07-02 16:00:00",
    "2026-07-02T16:00:00+00:00",
    "2026-07-02T16:00:00Z",
    "2026-07-02",
])
def test_parse_dt_handles_every_stored_format(raw):
    assert tax.parse_dt(raw).year == 2026


def test_parse_dt_returns_none_for_junk():
    assert tax.parse_dt(None) is None
    assert tax.parse_dt("") is None
    assert tax.parse_dt("not a date") is None


# ── The 61-day window ─────────────────────────────────────────────────────────

def test_loss_with_repurchase_after_sale_is_a_wash_sale():
    trades = [
        _trade(1, "NVDA", "2026-03-01", "2026-03-05", exit_px=90.0),   # -$100
        _trade(2, "NVDA", "2026-03-10", "2026-03-20", exit_px=105.0),  # replacement
    ]
    recs = {r.trade_id: r for r in tax.compute_tax_records(trades)}
    assert recs[1].is_wash_sale is True
    assert recs[1].disallowed_loss == pytest.approx(100.0)
    assert recs[1].replacement_trade_id == 2
    assert recs[1].deductible_pnl == pytest.approx(0.0)


def test_purchase_BEFORE_the_loss_sale_also_washes_it():
    """The backward-looking half of the window is the part people miss."""
    trades = [
        _trade(1, "NVDA", "2026-03-01", "2026-03-06", exit_px=105.0),
        _trade(2, "NVDA", "2026-03-20", "2026-03-25", exit_px=90.0),   # the loss
    ]
    recs = {r.trade_id: r for r in tax.compute_tax_records(trades)}
    assert recs[2].is_wash_sale is True
    assert recs[2].replacement_trade_id == 1


def test_repurchase_on_day_31_is_clean():
    trades = [
        _trade(1, "NVDA", "2026-03-01", "2026-03-05", exit_px=90.0),
        _trade(2, "NVDA", "2026-04-05", "2026-04-10", exit_px=105.0),
    ]
    recs = {r.trade_id: r for r in tax.compute_tax_records(trades)}
    assert recs[1].is_wash_sale is False
    assert recs[1].deductible_pnl == pytest.approx(-100.0)


def test_repurchase_on_day_30_still_washes():
    trades = [
        _trade(1, "NVDA", "2026-03-01", "2026-03-05", exit_px=90.0),
        _trade(2, "NVDA", "2026-04-04", "2026-04-10", exit_px=105.0),
    ]
    recs = {r.trade_id: r for r in tax.compute_tax_records(trades)}
    assert recs[1].is_wash_sale is True


def test_a_different_ticker_is_not_substantially_identical():
    trades = [
        _trade(1, "NVDA", "2026-03-01", "2026-03-05", exit_px=90.0),
        _trade(2, "AMD", "2026-03-06", "2026-03-10", exit_px=105.0),
    ]
    recs = {r.trade_id: r for r in tax.compute_tax_records(trades)}
    assert recs[1].is_wash_sale is False


def test_gains_are_never_wash_sales():
    trades = [
        _trade(1, "NVDA", "2026-03-01", "2026-03-05", exit_px=110.0),  # +$100
        _trade(2, "NVDA", "2026-03-06", "2026-03-10", exit_px=115.0),
    ]
    recs = {r.trade_id: r for r in tax.compute_tax_records(trades)}
    assert recs[1].is_wash_sale is False
    assert recs[1].deductible_pnl == pytest.approx(100.0)


def test_an_open_position_can_be_the_replacement():
    trades = [
        _trade(1, "NVDA", "2026-03-01", "2026-03-05", exit_px=90.0),
        _trade(2, "NVDA", "2026-03-08", None, status="open"),
    ]
    recs = {r.trade_id: r for r in tax.compute_tax_records(trades)}
    assert recs[1].is_wash_sale is True
    assert recs[1].replacement_trade_id == 2


def test_partial_replacement_disallows_proportionally():
    trades = [
        _trade(1, "NVDA", "2026-03-01", "2026-03-05", shares=10, exit_px=90.0),
        _trade(2, "NVDA", "2026-03-08", "2026-03-12", shares=4, exit_px=95.0),
    ]
    recs = {r.trade_id: r for r in tax.compute_tax_records(trades)}
    assert recs[1].disallowed_loss == pytest.approx(40.0)   # 4/10 of $100
    assert recs[1].deductible_pnl == pytest.approx(-60.0)


def test_broker_fill_price_preferred_over_signal_price():
    t = _trade(1, "NVDA", "2026-03-01", "2026-03-05", shares=10, exit_px=110.0)
    t["entry_filled_price"] = 101.0
    rec = tax.compute_tax_records([t])[0]
    assert rec.cost_basis == pytest.approx(1010.0)
    assert rec.realized_pnl == pytest.approx(90.0)


# ── Term classification ───────────────────────────────────────────────────────

def test_short_hold_is_short_term():
    rec = tax.compute_tax_records(
        [_trade(1, "NVDA", "2026-03-01", "2026-03-05", exit_px=110.0)]
    )[0]
    assert rec.term == "short"


def test_hold_over_one_year_is_long_term():
    rec = tax.compute_tax_records(
        [_trade(1, "NVDA", "2024-01-01", "2025-06-01", exit_px=110.0)]
    )[0]
    assert rec.term == "long"


def test_exactly_365_days_is_still_short_term():
    """Long-term requires MORE than a year."""
    rec = tax.compute_tax_records(
        [_trade(1, "NVDA", "2025-01-01", "2026-01-01", exit_px=110.0)]
    )[0]
    assert rec.term == "short"


# ── Year-end straddle ─────────────────────────────────────────────────────────

def test_wash_sale_inside_one_year_does_not_straddle():
    trades = [
        _trade(1, "NVDA", "2026-03-01", "2026-03-05", exit_px=90.0),
        _trade(2, "NVDA", "2026-03-08", "2026-03-20", exit_px=95.0),
    ]
    recs = {r.trade_id: r for r in tax.compute_tax_records(trades)}
    assert recs[1].is_wash_sale is True
    assert recs[1].straddles_year_end is False


def test_december_loss_with_replacement_open_at_year_end_straddles():
    trades = [
        _trade(1, "NVDA", "2026-12-20", "2026-12-22", exit_px=90.0),
        _trade(2, "NVDA", "2026-12-28", "2027-01-15", exit_px=95.0),
    ]
    recs = {r.trade_id: r for r in tax.compute_tax_records(trades)}
    assert recs[1].straddles_year_end is True


# ── Forecast ──────────────────────────────────────────────────────────────────

def test_summary_taxes_short_and_long_at_their_own_rates():
    trades = [
        _trade(1, "NVDA", "2026-01-01", "2026-01-10", shares=10, exit_px=110.0),
        _trade(2, "AMD", "2024-01-01", "2026-06-01", shares=10, exit_px=110.0),
    ]
    recs = tax.compute_tax_records(trades)
    s = tax.summarize_year(recs, 2026, 0.24, 0.15)
    assert s["short_term_net"] == pytest.approx(100.0)
    assert s["long_term_net"] == pytest.approx(100.0)
    assert s["estimated_tax"] == pytest.approx(100 * 0.24 + 100 * 0.15)


def test_disallowed_loss_raises_taxable_income():
    """A washed loss cannot shelter a gain in the same year."""
    trades = [
        _trade(1, "NVDA", "2026-01-01", "2026-01-10", shares=10, exit_px=90.0),
        _trade(2, "NVDA", "2026-01-12", "2026-01-20", shares=10, exit_px=100.0),
        _trade(3, "AMD", "2026-02-01", "2026-02-10", shares=10, exit_px=110.0),
    ]
    s = tax.summarize_year(tax.compute_tax_records(trades), 2026, 0.24, 0.15)
    assert s["wash_sale_count"] == 1
    assert s["disallowed_loss_total"] == pytest.approx(100.0)
    # gross is 0 (-100 +0 +100) but the washed loss is not deductible
    assert s["gross_realized_pnl"] == pytest.approx(0.0)
    assert s["net_capital_gain"] == pytest.approx(100.0)
    assert s["estimated_tax"] == pytest.approx(24.0)


def test_net_loss_is_capped_at_the_annual_deduction_limit():
    trades = [_trade(1, "NVDA", "2026-01-01", "2026-01-10", shares=1000, exit_px=95.0)]
    s = tax.summarize_year(tax.compute_tax_records(trades), 2026, 0.24, 0.15)
    assert s["net_capital_gain"] == pytest.approx(-5000.0)
    assert s["estimated_tax"] == 0.0
    assert s["loss_deductible_this_year"] == pytest.approx(3000.0)
    assert s["loss_carryforward"] == pytest.approx(2000.0)


def test_short_term_loss_offsets_long_term_gain_before_tax():
    trades = [
        _trade(1, "NVDA", "2026-01-01", "2026-01-10", shares=10, exit_px=90.0),
        _trade(2, "AMD", "2024-01-01", "2026-06-01", shares=10, exit_px=130.0),
    ]
    s = tax.summarize_year(tax.compute_tax_records(trades), 2026, 0.24, 0.15)
    assert s["short_term_net"] == pytest.approx(-100.0)
    assert s["long_term_net"] == pytest.approx(300.0)
    assert s["estimated_tax"] == pytest.approx(200 * 0.15)


def test_other_years_are_excluded():
    trades = [
        _trade(1, "NVDA", "2025-01-01", "2025-01-10", shares=10, exit_px=110.0),
        _trade(2, "NVDA", "2026-01-01", "2026-01-10", shares=10, exit_px=120.0),
    ]
    s = tax.summarize_year(tax.compute_tax_records(trades), 2026, 0.24, 0.15)
    assert s["trades_closed"] == 1
    assert s["short_term_net"] == pytest.approx(200.0)


# ── Year-end entry guard ──────────────────────────────────────────────────────

def test_guard_blocks_december_reentry_after_a_loss():
    trades = [_trade(1, "NVDA", "2026-12-01", "2026-12-10", exit_px=90.0)]
    reason = tax.year_end_entry_block("NVDA", _d("2026-12-15"), trades)
    assert reason is not None
    assert "NVDA" in reason


def test_guard_is_silent_outside_december():
    """100% of this bot's losing trades are washed intra-year; that is fine."""
    trades = [_trade(1, "NVDA", "2026-06-01", "2026-06-10", exit_px=90.0)]
    assert tax.year_end_entry_block("NVDA", _d("2026-06-15"), trades) is None


def test_guard_allows_reentry_once_31_days_have_passed():
    trades = [_trade(1, "NVDA", "2026-12-01", "2026-12-02", exit_px=90.0)]
    assert tax.year_end_entry_block("NVDA", _d("2027-01-03"), trades) is None


# The January tail. A Dec 20 loss is still washed by a Jan 10 repurchase — 21
# days later, inside the 30-day window — and buying then pulls a deduction out
# of the prior tax year. The guard must stay armed across the year boundary.

@pytest.mark.parametrize("when,blocked", [
    ("2026-12-28", True),    # 8 days after  — December side
    ("2027-01-10", True),    # 21 days after — the case that regressed
    ("2027-01-19", True),    # 30 days after — last blocked day
    ("2027-01-20", False),   # 31 days after — safe re-entry
    ("2027-02-01", False),   # well clear
])
def test_guard_spans_the_year_boundary(when, blocked):
    trades = [_trade(1, "NVDA", "2026-12-15", "2026-12-20", exit_px=90.0)]
    reason = tax.year_end_entry_block("NVDA", _d(when), trades)
    assert (reason is not None) is blocked


def test_january_block_names_the_year_the_deduction_would_leave():
    trades = [_trade(1, "NVDA", "2026-12-15", "2026-12-20", exit_px=90.0)]
    reason = tax.year_end_entry_block("NVDA", _d("2027-01-10"), trades)
    assert "2026 tax year" in reason


def test_january_tail_ignores_a_prior_year_gain():
    trades = [_trade(1, "NVDA", "2026-12-15", "2026-12-20", exit_px=110.0)]
    assert tax.year_end_entry_block("NVDA", _d("2027-01-10"), trades) is None


def test_guard_ignores_profitable_trades():
    trades = [_trade(1, "NVDA", "2026-12-01", "2026-12-10", exit_px=110.0)]
    assert tax.year_end_entry_block("NVDA", _d("2026-12-15"), trades) is None


def test_guard_ignores_other_tickers():
    trades = [_trade(1, "AMD", "2026-12-01", "2026-12-10", exit_px=90.0)]
    assert tax.year_end_entry_block("NVDA", _d("2026-12-15"), trades) is None


def test_guard_can_be_disabled():
    trades = [_trade(1, "NVDA", "2026-12-01", "2026-12-10", exit_px=90.0)]
    assert tax.year_end_entry_block(
        "NVDA", _d("2026-12-15"), trades, enabled=False
    ) is None
