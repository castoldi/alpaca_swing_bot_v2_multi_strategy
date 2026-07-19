"""Leveraged-ETF exposure cap — sizing, backtest engine, and live bot.

The account's position limit is count-based (5 x 20% = 100% of equity) and so
cannot see correlation. Leveraged ETFs move and signal together, so without a
group cap the whole account could end up in 3x instruments at once. These tests
pin that cap down in every path that can open a position.
"""
from __future__ import annotations

import math

import pandas as pd
import pytest

from config import PARAMS
from position_sizing import leveraged_headroom, whole_share_position_size
from backtest_portfolio import BacktestCandidate, run_annual_portfolio
from strategies.base import ExitLeg


# ── headroom ─────────────────────────────────────────────────────────────────

def test_headroom_is_cap_minus_open():
    assert leveraged_headroom(1000.0, 0.0, 0.20) == pytest.approx(200.0)
    assert leveraged_headroom(1000.0, 150.0, 0.20) == pytest.approx(50.0)


def test_headroom_never_negative_when_over_cap():
    assert leveraged_headroom(1000.0, 500.0, 0.20) == 0.0


def test_headroom_zero_on_bad_input():
    assert leveraged_headroom(0.0, 0.0, 0.20) == 0.0
    assert leveraged_headroom(-1.0, 0.0, 0.20) == 0.0
    assert leveraged_headroom(float("nan"), 0.0, 0.20) == 0.0
    assert leveraged_headroom(1000.0, float("inf"), 0.20) == 0.0


# ── sizing ───────────────────────────────────────────────────────────────────

def test_max_notional_caps_quantity():
    uncapped = whole_share_position_size(10_000.0, 10_000.0, 100.0, 0.20)
    assert uncapped.quantity == 20
    capped = whole_share_position_size(
        10_000.0, 10_000.0, 100.0, 0.20, max_notional=550.0
    )
    assert capped.quantity == 5
    assert capped.notional == pytest.approx(500.0)


def test_max_notional_below_one_share_is_refused_with_reason():
    size = whole_share_position_size(
        10_000.0, 10_000.0, 100.0, 0.20, max_notional=99.0
    )
    assert size.quantity == 0
    assert size.reason == "group_exposure_cap"


def test_zero_headroom_refuses_entry():
    size = whole_share_position_size(
        10_000.0, 10_000.0, 100.0, 0.20, max_notional=0.0
    )
    assert size.quantity == 0
    assert size.reason == "group_exposure_cap"


def test_infinite_headroom_is_rejected_not_treated_as_unlimited():
    """_open_leveraged_notional returns inf when a position is unreadable."""
    size = whole_share_position_size(
        10_000.0, 10_000.0, 100.0, 0.20, max_notional=float("inf")
    )
    assert size.quantity == 0
    assert size.reason == "invalid_input"


def test_max_notional_none_preserves_existing_behaviour():
    a = whole_share_position_size(10_000.0, 10_000.0, 100.0, 0.20)
    b = whole_share_position_size(10_000.0, 10_000.0, 100.0, 0.20, max_notional=None)
    assert a == b


def test_cap_does_not_loosen_the_cash_limit():
    """A generous group cap must not let sizing exceed available cash."""
    size = whole_share_position_size(
        10_000.0, 300.0, 100.0, 0.20, max_notional=99_999.0
    )
    assert size.quantity == 3


# ── backtest engine ──────────────────────────────────────────────────────────

def _candidate(ticker: str, day: int, price: float = 100.0, exit_price: float = 101.0):
    entry = pd.Timestamp("2025-01-01") + pd.Timedelta(days=day)
    leg = ExitLeg(entry + pd.Timedelta(days=30), exit_price, "take_profit", 30, 1.0)
    return BacktestCandidate(
        ticker=ticker,
        entry_date=entry,
        entry_price=price,
        stop_loss=price * 0.9,
        take_profit=exit_price,
        strategy="tqqq_momentum",
        single_legs=(leg,),
        scaled_legs=(leg,),
    )


def _run(candidates, **kwargs):
    return run_annual_portfolio(
        candidates,
        initial_equity=10_000.0,
        position_fraction=0.20,
        max_positions=5,
        **kwargs,
    )


def test_backtest_caps_total_leveraged_notional():
    """Four correlated leveraged entries on the same day; only 20% gets in."""
    leveraged = frozenset({"TQQQ", "SOXL", "TECL", "UPRO"})
    cands = [_candidate(t, 0) for t in sorted(leveraged)]
    result = _run(cands, leveraged_tickers=leveraged, max_leveraged_fraction=0.20)

    notional = sum(t.shares * t.entry_price for t in result.trades)
    assert notional <= 10_000.0 * 0.20 + 1e-6
    assert result.accepted_positions == 1
    assert result.skipped_positions == 3


def test_backtest_allows_more_when_cap_is_raised():
    leveraged = frozenset({"TQQQ", "SOXL", "TECL", "UPRO"})
    cands = [_candidate(t, 0) for t in sorted(leveraged)]
    result = _run(cands, leveraged_tickers=leveraged, max_leveraged_fraction=0.60)

    notional = sum(t.shares * t.entry_price for t in result.trades)
    assert notional <= 10_000.0 * 0.60 + 1e-6
    assert result.accepted_positions == 3


def test_backtest_leaves_unleveraged_tickers_uncapped():
    """The cap must not touch the ordinary equity universe."""
    cands = [_candidate(t, 0) for t in ("NVDA", "AMZN", "META", "AMD")]
    result = _run(cands, leveraged_tickers=frozenset({"TQQQ"}), max_leveraged_fraction=0.20)
    assert result.accepted_positions == 4


def test_backtest_releases_headroom_after_exit():
    """A closed leveraged position frees its exposure for a later entry."""
    leveraged = frozenset({"TQQQ", "SOXL"})
    # Second entry is 60 days later — the first (30-day hold) has closed.
    cands = [_candidate("TQQQ", 0), _candidate("SOXL", 60)]
    result = _run(cands, leveraged_tickers=leveraged, max_leveraged_fraction=0.20)
    assert result.accepted_positions == 2
    assert {t.ticker for t in result.trades} == {"TQQQ", "SOXL"}


def test_backtest_default_cap_matches_config():
    """Called without overrides, the engine uses the configured cap."""
    leveraged_default = _run([_candidate("TQQQ", 0)])
    assert leveraged_default.accepted_positions == 1
    notional = sum(t.shares * t.entry_price for t in leveraged_default.trades)
    assert notional <= 10_000.0 * PARAMS.max_leveraged_exposure_pct + 1e-6


def test_single_leveraged_ticker_behaviour_is_unchanged():
    """Today's universe (one leveraged ticker) must size exactly as before."""
    cands = [_candidate("TQQQ", 0)]
    with_cap = _run(cands, leveraged_tickers=frozenset({"TQQQ"}), max_leveraged_fraction=0.20)
    without = _run(cands, leveraged_tickers=frozenset(), max_leveraged_fraction=0.20)
    assert [t.shares for t in with_cap.trades] == [t.shares for t in without.trades]


# ── live bot ─────────────────────────────────────────────────────────────────

class _Pos:
    def __init__(self, symbol, market_value):
        self.symbol = symbol
        self.market_value = market_value


def test_open_leveraged_notional_sums_only_leveraged():
    import bot

    total = bot._open_leveraged_notional(
        [_Pos("TQQQ", "150.0"), _Pos("NVDA", "900.0")]
    )
    assert total == pytest.approx(150.0)


def test_open_leveraged_notional_ignores_empty_positions():
    import bot

    assert bot._open_leveraged_notional([]) == 0.0
    assert bot._open_leveraged_notional(None) == 0.0


def test_open_leveraged_notional_fails_closed_on_unreadable_value():
    """An unreadable leveraged position must block, not free, headroom."""
    import bot

    total = bot._open_leveraged_notional([_Pos("TQQQ", None)])
    assert math.isinf(total)
    assert leveraged_headroom(10_000.0, total, 0.20) == 0.0


def test_live_sizing_state_reports_leveraged_notional():
    import bot

    class _Account:
        equity = "10000"
        cash = "10000"
        last_equity = "10000"

    class _Client:
        @staticmethod
        def get_account():
            return _Account()

        @staticmethod
        def get_all_positions():
            return [_Pos("TQQQ", "150.0"), _Pos("AMD", "500.0")]

    state = bot._load_live_sizing(_Client())
    assert state is not None
    assert state.leveraged_notional == pytest.approx(150.0)
    assert leveraged_headroom(
        state.equity, state.leveraged_notional, 0.20
    ) == pytest.approx(1850.0)
