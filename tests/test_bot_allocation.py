"""Virtual capital allocation: the bot sizes against its own slice of a shared key.

Before this, sizing read account-wide equity and cash, so at 5 x 20% the bot
could deploy 100% of an Alpaca account that 8 other projects also trade
(it held $114k of a $115k account on 2026-09-26).
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

import bot


class _Client:
    def __init__(self, equity, cash, positions=()):
        self.equity, self.cash, self.positions = equity, cash, list(positions)

    def get_account(self):
        return SimpleNamespace(equity=str(self.equity), cash=str(self.cash),
                               last_equity=str(self.equity))

    def get_all_positions(self):
        return [SimpleNamespace(symbol=s, qty="10", market_value="20000")
                for s in self.positions]


def _setup(monkeypatch, allocation, open_trades=()):
    monkeypatch.setattr(bot, "PARAMS", replace(bot.PARAMS, bot_capital_allocation=allocation))
    monkeypatch.setattr(bot.db_mod, "get_open_trades", lambda: list(open_trades))
    monkeypatch.setattr(bot, "_bot_daily_loss_pct", lambda _account: 0.0)


def _trade(ticker, shares, fill):
    return {"id": 1, "ticker": ticker, "shares": shares, "entry_filled_price": fill,
            "entry_price": fill, "strategy": "ensemble", "entry_state": "filled"}


def test_sizing_base_is_the_allocation_not_the_shared_account(monkeypatch):
    _setup(monkeypatch, 100_000)
    state = bot._load_live_sizing(_Client(equity=400_000, cash=400_000))
    assert state.equity == 100_000
    assert state.remaining_cash == 100_000


def test_spendable_is_allocation_minus_own_open_cost_basis(monkeypatch):
    trades = [_trade("NVDA", 100, 200.0), _trade("AMD", 50, 400.0), _trade("META", 25, 800.0)]
    _setup(monkeypatch, 100_000, trades)
    state = bot._load_live_sizing(_Client(equity=400_000, cash=300_000,
                                          positions=["NVDA", "AMD", "META"]))
    assert state.remaining_cash == pytest.approx(40_000)
    assert state.remaining_slots == 2


def test_real_account_cash_still_gates(monkeypatch):
    """Another project may have spent the cash: never size past what exists."""
    _setup(monkeypatch, 100_000)
    state = bot._load_live_sizing(_Client(equity=400_000, cash=5_000))
    assert state.remaining_cash == 5_000


def test_allocation_never_exceeds_account_equity(monkeypatch):
    _setup(monkeypatch, 100_000)
    state = bot._load_live_sizing(_Client(equity=60_000, cash=60_000))
    assert state.equity == 60_000


def test_zero_allocation_restores_account_wide_sizing(monkeypatch):
    _setup(monkeypatch, 0)
    state = bot._load_live_sizing(_Client(equity=400_000, cash=300_000))
    assert (state.equity, state.remaining_cash) == (400_000, 300_000)


def test_unreadable_ledger_leaves_nothing_spendable(monkeypatch):
    _setup(monkeypatch, 100_000)

    def boom():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(bot.db_mod, "get_open_trades", boom)
    state = bot._load_live_sizing(_Client(equity=400_000, cash=400_000))
    assert state.remaining_cash == 0.0


def test_twenty_percent_slot_is_twenty_percent_of_allocation(monkeypatch):
    _setup(monkeypatch, 100_000)
    state = bot._load_live_sizing(_Client(equity=400_000, cash=400_000))
    size = bot.whole_share_position_size(state.equity, state.remaining_cash, 250.0,
                                         bot.PARAMS.position_size_pct)
    assert size.quantity == 80  # $20,000 / $250, not $80,000 / $250


def test_ledger_base_follows_allocation(monkeypatch):
    _setup(monkeypatch, 100_000)
    assert bot._ledger_capital_base() == 100_000
    _setup(monkeypatch, 0)
    assert bot._ledger_capital_base() is None
