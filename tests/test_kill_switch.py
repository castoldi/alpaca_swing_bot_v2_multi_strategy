"""Daily-loss kill switch in the backtest engine.

Mirrors bot.py's live check: Alpaca's mark-to-market account.equity right now
vs. yesterday's closing equity, blocked once the drop reaches
max_daily_loss_pct. Every scenario here was hand-traced against a debug script
before being encoded as an assertion — the day-boundary lookup originally used
`Timedelta(nanoseconds=1)` to mean "just before midnight", which raised inside
pandas whenever a frame's DatetimeIndex wasn't nanosecond-resolution (a
microsecond- or second-resolution index is exactly what a real cached frame
often is) and was silently swallowed into a wrong answer by an overly broad
except clause. Fixed with boolean-mask lookups instead of `Series.asof`.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backtest_portfolio import (
    BacktestCandidate,
    _mark_to_market_equity,
    _price_asof,
    run_annual_portfolio,
)
from strategies.base import ExitLeg


def _single_candidate(ticker, entry_date, entry_price, exit_date, exit_price):
    reason = "take_profit" if exit_price >= entry_price else "stop_loss"
    leg = ExitLeg(pd.Timestamp(exit_date), float(exit_price), reason, 1, 1.0)
    return BacktestCandidate(
        ticker=ticker, entry_date=pd.Timestamp(entry_date),
        entry_price=float(entry_price), stop_loss=float(entry_price) * 0.9,
        take_profit=float(exit_price), strategy="test",
        single_legs=(leg,), scaled_legs=(leg,),
    )


def _frame(pairs):
    """pairs: [(timestamp_str, close), ...]"""
    idx = pd.to_datetime([p[0] for p in pairs])
    return pd.DataFrame({"close": [p[1] for p in pairs]}, index=idx)


# ── _price_asof / _mark_to_market_equity ──────────────────────────────────────

def test_price_asof_handles_a_non_nanosecond_index():
    # Regression: pd.to_datetime on a plain string list commonly yields
    # datetime64[us] or [s], not [ns]. Must not raise or silently return None.
    frame = _frame([("2026-01-02 16:00", 100.0), ("2026-01-02 20:00", 105.0)])
    assert str(frame.index.dtype) != "datetime64[ns]"

    assert _price_asof(frame, pd.Timestamp("2026-01-03"), strict=True) == 105.0
    assert _price_asof(frame, pd.Timestamp("2026-01-02 20:00")) == 105.0


def test_price_asof_strict_excludes_the_exact_timestamp():
    frame = _frame([("2026-01-02 20:00", 105.0)])
    assert _price_asof(frame, pd.Timestamp("2026-01-02 20:00"), strict=True) is None
    assert _price_asof(frame, pd.Timestamp("2026-01-02 20:00"), strict=False) == 105.0


def test_price_asof_returns_none_before_any_data():
    frame = _frame([("2026-01-02 16:00", 100.0)])
    assert _price_asof(frame, pd.Timestamp("2026-01-01")) is None


def test_mark_to_market_equity_sums_cash_and_open_positions():
    frame = _frame([("2026-01-02 16:00", 50.0)])
    equity = _mark_to_market_equity(
        cash=800.0, position_tickers={1: "TICK"}, open_remaining={1: 4},
        price_frames={"TICK": frame}, as_of=pd.Timestamp("2026-01-02 16:00"),
    )
    assert equity == 1000.0   # 800 cash + 4 * 50


def test_mark_to_market_equity_ignores_a_position_with_no_frame():
    # Missing frames contribute 0 on both sides of a later delta, rather than
    # crashing - acceptable for callers that only have partial data, though
    # production always supplies the full universe.
    equity = _mark_to_market_equity(
        cash=800.0, position_tickers={1: "UNKNOWN"}, open_remaining={1: 4},
        price_frames={}, as_of=pd.Timestamp("2026-01-02 16:00"),
    )
    assert equity == 800.0


# ── The verified scenario ──────────────────────────────────────────────────────
#
# TICK enters day 1 at 100 (2 shares, cash 1000 -> 800). Day 2 crashes
# intraday to 70 (a candidate there must be blocked), recovers to 97 later the
# SAME day (a candidate there must be allowed - the live check is not sticky
# for the rest of the day, despite what its code comment says), and closes at
# 90. Day 3's baseline must come from day 2's close (90), not day 1's (100) -
# a candidate there is allowed only if the reset actually happened; a "never
# resets" bug would keep comparing against day 1 and wrongly block it.

_TICK_FRAME = _frame([
    ("2026-01-02 16:00", 100.0),   # day1 entry
    ("2026-01-02 20:00", 100.0),   # day1 EOD -> day2 baseline
    ("2026-01-03 12:00", 70.0),    # day2 crash bar
    ("2026-01-03 16:00", 97.0),    # day2 recovery bar (same day, <3% now)
    ("2026-01-03 20:00", 90.0),    # day2 EOD -> day3 baseline
    ("2026-01-04 12:00", 90.0),    # day3 bar
])


def _scenario_candidates():
    return [
        _single_candidate("TICK", "2026-01-02 16:00", 100, "2026-01-10", 100),
        _single_candidate("BLOCKED1", "2026-01-03 12:00", 50, "2026-01-10", 50),
        _single_candidate("ALLOWED1", "2026-01-03 16:00", 50, "2026-01-10", 50),
        _single_candidate("ALLOWED2", "2026-01-04 12:00", 50, "2026-01-10", 50),
    ]


def _run_scenario(**kwargs):
    return run_annual_portfolio(
        _scenario_candidates(), initial_equity=1000.0, position_fraction=0.20,
        max_positions=5, price_frames={"TICK": _TICK_FRAME}, apply_tax=False,
        **kwargs,
    )


def test_kill_switch_blocks_entry_during_an_intraday_breach():
    result = _run_scenario()
    tickers = {t.ticker for t in result.trades}
    assert "BLOCKED1" not in tickers


def test_kill_switch_is_not_sticky_for_the_rest_of_the_day():
    # Matches bot.py's actual (non-sticky) behaviour, not its comment.
    result = _run_scenario()
    tickers = {t.ticker for t in result.trades}
    assert "ALLOWED1" in tickers


def test_kill_switch_baseline_resets_to_yesterdays_close():
    # Would be wrongly blocked if the baseline stayed pinned to day 1.
    result = _run_scenario()
    tickers = {t.ticker for t in result.trades}
    assert "ALLOWED2" in tickers


def test_kill_switch_reports_one_blocked_entry_and_one_trip_day():
    result = _run_scenario()
    assert result.kill_switch_blocked_entries == 1
    assert result.kill_switch_trip_days == 1


def test_exits_are_never_gated_by_the_kill_switch():
    # TICK's own exit is pre-resolved at candidate-collection time and must
    # fire regardless of what the kill switch is doing to NEW entries.
    result = _run_scenario()
    tick_trade = next(t for t in result.trades if t.ticker == "TICK")
    assert tick_trade.exit_reason == "take_profit"


# ── Defaults and validation ───────────────────────────────────────────────────

def test_kill_switch_defaults_off_without_price_frames():
    result = run_annual_portfolio(
        _scenario_candidates(), initial_equity=1000.0, position_fraction=0.20,
        max_positions=5, apply_tax=False,
    )
    tickers = {t.ticker for t in result.trades}
    assert "BLOCKED1" in tickers   # nothing blocks it when the guard is off
    assert result.kill_switch_blocked_entries == 0


def test_apply_kill_switch_true_requires_price_frames():
    with pytest.raises(ValueError):
        run_annual_portfolio(
            _scenario_candidates(), initial_equity=1000.0,
            position_fraction=0.20, max_positions=5,
            apply_kill_switch=True, apply_tax=False,
        )


def test_apply_kill_switch_can_be_forced_off_even_with_frames():
    result = _run_scenario(apply_kill_switch=False)
    tickers = {t.ticker for t in result.trades}
    assert "BLOCKED1" in tickers
    assert result.kill_switch_blocked_entries == 0


def test_threshold_is_configurable():
    # A much looser threshold never trips on the same data.
    result = _run_scenario(max_daily_loss_pct=0.50)
    tickers = {t.ticker for t in result.trades}
    assert "BLOCKED1" in tickers
    assert result.kill_switch_blocked_entries == 0
