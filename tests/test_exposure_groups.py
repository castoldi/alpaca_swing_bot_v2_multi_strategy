"""Correlated-group exposure caps (R04 in docs/code-review-2026-09-22.md).

The generalization of the leveraged-ETF cap: total notional across a declared
group of correlated tickers stays under a fraction of equity, identically in
live sizing and in the annual backtest. The default (no groups) must change
nothing.
"""
from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

import bot
from config import PARAMS, StrategyType
from backtest_portfolio import BacktestCandidate, run_annual_portfolio
from position_sizing import combine_headroom, group_headroom
from strategies.base import ExitLeg
from tests.test_bot_shared_account import _SharedAccountClient, _cycle

SEMIS = (("semis", ("NVDA", "AMD", "ARM"), 0.30),)


# ── headroom ─────────────────────────────────────────────────────────────────

def test_group_headroom_is_cap_minus_group_notional():
    open_ = {"NVDA": 200.0, "AMD": 50.0, "META": 900.0}
    assert group_headroom(1000.0, "ARM", open_, SEMIS) == pytest.approx(50.0)


def test_ticker_outside_every_group_has_no_group_limit():
    assert group_headroom(1000.0, "META", {"NVDA": 999.0}, SEMIS) is None


def test_tightest_group_wins():
    groups = SEMIS + (("chips_small", ("AMD", "ARM"), 0.10),)
    assert group_headroom(1000.0, "AMD", {"ARM": 60.0}, groups) == pytest.approx(40.0)


def test_unreadable_group_member_leaves_no_headroom():
    assert group_headroom(1000.0, "AMD", {"NVDA": float("inf")}, SEMIS) == 0.0


def test_combine_headroom():
    assert combine_headroom(None, None) is None
    assert combine_headroom(None, 50.0) == 50.0
    assert combine_headroom(80.0, 50.0) == 50.0


# ── backtest engine ──────────────────────────────────────────────────────────

def _candidate(ticker, day, price=100.0, hold_days=30):
    entry = pd.Timestamp("2025-01-01") + pd.Timedelta(days=day)
    leg = ExitLeg(entry + pd.Timedelta(days=hold_days), price * 1.01, "take_profit", 30, 1.0)
    return BacktestCandidate(ticker=ticker, entry_date=entry, entry_price=price,
                             stop_loss=price * 0.9, take_profit=price * 1.01,
                             strategy="ensemble", single_legs=(leg,), scaled_legs=(leg,))


def _run(candidates, groups=()):
    return run_annual_portfolio(
        candidates, initial_equity=10_000.0, position_fraction=0.20, max_positions=5,
        params=replace(PARAMS, exposure_groups=groups),
    )


def test_backtest_caps_a_correlated_group_on_one_bar():
    """The 2026-09-21 shape: several semis signal on the same bar."""
    result = _run([_candidate(t, 0) for t in ("AMD", "ARM", "NVDA", "META")], SEMIS)

    semis = sum(t.shares * t.entry_price for t in result.trades if t.ticker != "META")
    assert semis <= 10_000.0 * 0.30 + 1e-6
    assert {t.ticker for t in result.trades} == {"AMD", "ARM", "META"}
    assert [t.shares for t in result.trades if t.ticker == "ARM"] == [10]   # partial


def test_backtest_releases_group_headroom_after_exit():
    cands = [_candidate("NVDA", 0), _candidate("AMD", 1), _candidate("ARM", 60)]
    result = _run(cands, (("semis", ("NVDA", "AMD", "ARM"), 0.20),))
    assert [t.ticker for t in result.trades] == ["NVDA", "ARM"]


def test_no_groups_changes_nothing():
    cands = [_candidate(t, 0) for t in ("AMD", "ARM", "NVDA", "META")]
    default = run_annual_portfolio(cands, initial_equity=10_000.0, position_fraction=0.20,
                                   max_positions=5)
    assert [(t.ticker, t.shares) for t in _run(cands).trades] == \
           [(t.ticker, t.shares) for t in default.trades]
    assert PARAMS.exposure_groups == ()


# ── live bot ─────────────────────────────────────────────────────────────────

class _Pos:
    def __init__(self, symbol, market_value):
        self.symbol, self.market_value = symbol, market_value


def test_open_notional_by_ticker_is_account_wide_and_fails_closed():
    out = bot._open_notional_by_ticker([_Pos("NVDA", "-900"), _Pos("SPY", "100"),
                                        _Pos("AMD", None)])
    assert out["NVDA"] == 900.0 and out["SPY"] == 100.0
    assert out["AMD"] == float("inf")


@pytest.mark.parametrize("cap, expected", [
    (0.30, [("NVDA", 200), ("AMD", 100)]),   # second entry gets the remaining 10%
    (0.20, [("NVDA", 200)]),                 # group full after the first entry
])
def test_live_cycle_reserves_group_exposure_between_tickers(monkeypatch, cap, expected):
    placed = _cycle(monkeypatch, _SharedAccountClient(), ["NVDA", "AMD"])
    monkeypatch.setattr(bot, "PARAMS", replace(
        bot.PARAMS, exposure_groups=(("semis", ("NVDA", "AMD"), cap),)))
    monkeypatch.setattr(bot.data_feed, "fetch_snapshots",
                        lambda symbols: {symbols[0]: {"price": 100.0}})

    bot.run_once(StrategyType.ENSEMBLE)

    assert placed == expected
