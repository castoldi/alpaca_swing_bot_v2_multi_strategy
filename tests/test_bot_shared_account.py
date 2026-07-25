"""Guards for a shared Alpaca key and for refusing duplicate entries.

Two related failure modes, both about the bot correctly understanding what it
owns:

1. The key is shared with another bot (day-trading SPY/QQQ). Its positions must
   not consume this bot's position slots, but shared cash/equity must stay
   account-wide.
2. When this bot already holds a ticker — in its own DB or at the broker — a
   fresh signal must not place a second order. `bot.py` has guarded this since
   0.12.0 but nothing pinned the behaviour down, so a refactor could drop it
   silently. Trades 14-34 in the live DB are what that costs: 21 real NVDA buy
   orders for one signal.
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

import bot
from config import StrategyType
from strategies.base import EntrySignal


class _NoPositionError(RuntimeError):
    """Alpaca's 404 for get_open_position on a symbol with no position."""
    status_code = 404


class _LookupFailed(RuntimeError):
    """A non-404 failure: real state is unknown, so entries must fail closed."""
    status_code = 500


class _SharedAccountClient:
    """Account holding this bot's positions plus another bot's SPY/QQQ."""

    def __init__(self, our_symbols=(), foreign_symbols=(), position_error=None):
        self.our_symbols = list(our_symbols)
        self.foreign_symbols = list(foreign_symbols)
        self.position_error = position_error

    def get_account(self):
        return SimpleNamespace(equity="100000", cash="100000", last_equity="100000")

    def get_all_positions(self):
        return [
            SimpleNamespace(symbol=sym, qty="10", market_value="1000")
            for sym in self.our_symbols + self.foreign_symbols
        ]

    def get_open_position(self, ticker):
        if self.position_error is not None:
            raise self.position_error
        if ticker in self.our_symbols + self.foreign_symbols:
            return SimpleNamespace(symbol=ticker, qty="10")
        raise _NoPositionError("position does not exist")

    def get_order_by_id(self, _order_id):
        raise RuntimeError("order lookup unavailable")


def _owned(monkeypatch, tickers):
    monkeypatch.setattr(
        bot.db_mod,
        "get_open_trades",
        lambda: [
            {"ticker": t, "strategy": "ensemble", "entry_state": "filled"}
            for t in tickers
        ],
    )


# ── Slot accounting under a shared key ────────────────────────────────────────

def test_other_bots_positions_do_not_consume_our_slots(monkeypatch):
    """The SPY/QQQ day-trader must not eat this bot's five slots."""
    _owned(monkeypatch, ["NVDA"])
    client = _SharedAccountClient(
        our_symbols=["NVDA"], foreign_symbols=["SPY", "QQQ"]
    )

    state = bot._load_live_sizing(client)

    # 3 account positions, but only NVDA is ours -> 4 slots left, not 2.
    assert state.remaining_slots == 4


def test_account_full_of_foreign_positions_still_leaves_us_capacity(monkeypatch):
    """Five foreign positions previously zeroed our capacity silently."""
    _owned(monkeypatch, [])
    client = _SharedAccountClient(
        foreign_symbols=["SPY", "QQQ", "IWM", "DIA", "XLF"]
    )

    state = bot._load_live_sizing(client)

    assert state.remaining_slots == 5


def test_our_own_positions_still_consume_slots(monkeypatch):
    _owned(monkeypatch, ["NVDA", "AMD", "ARM", "AMZN", "META"])
    client = _SharedAccountClient(
        our_symbols=["NVDA", "AMD", "ARM", "AMZN", "META"],
        foreign_symbols=["SPY"],
    )

    state = bot._load_live_sizing(client)

    assert state.remaining_slots == 0


def test_unreadable_ownership_fails_closed(monkeypatch):
    """If the DB cannot be read, charge every position to us (lose capacity)."""
    def boom():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(bot.db_mod, "get_open_trades", boom)
    client = _SharedAccountClient(
        our_symbols=["NVDA"], foreign_symbols=["SPY", "QQQ"]
    )

    state = bot._load_live_sizing(client)

    assert state.remaining_slots == 2   # 5 - 3, the conservative reading


def test_shared_cash_and_equity_stay_account_wide(monkeypatch):
    """Cash is genuinely shared - it must NOT be scoped to our positions."""
    _owned(monkeypatch, ["NVDA"])
    client = _SharedAccountClient(
        our_symbols=["NVDA"], foreign_symbols=["SPY", "QQQ"]
    )

    state = bot._load_live_sizing(client)

    assert state.equity == 100000.0
    assert state.remaining_cash == 100000.0


def test_counting_helper_ignores_symbolless_positions():
    positions = [SimpleNamespace(symbol=None), SimpleNamespace(symbol="NVDA")]

    assert bot._our_open_position_count(positions, {"NVDA"}) == 1


# ── Duplicate-entry guards ────────────────────────────────────────────────────

class _EntryStrategy:
    name = "ensemble"
    timeframe = "4h"
    has_take_profit = True
    exit_mode = "bracket"

    @staticmethod
    def check_entry(frame, idx, params):
        return EntrySignal(
            date=pd.Timestamp(frame.index[idx]),
            entry_price=100.0,
            stop_loss=90.0,
            take_profit=110.0,
            atr=10.0,
            rsi=55.0,
            strategy="ensemble",
        )


def _frame():
    index = pd.date_range("2026-01-01", periods=60, freq="4h")
    return pd.DataFrame(
        {
            "open": [100.0] * 60,
            "high": [101.0] * 60,
            "low": [99.0] * 60,
            "close": [100.0] * 60,
            "volume": [1_000_000.0] * 60,
        },
        index=index,
    )


def _cycle(monkeypatch, client, tickers, open_trade=None):
    placed = []
    monkeypatch.setitem(bot.REGISTRY, "ensemble", _EntryStrategy())
    monkeypatch.setattr(bot, "TICKERS", list(tickers))
    monkeypatch.setattr(bot, "_get_trading", lambda: client)
    monkeypatch.setattr(bot, "fetch_bars", lambda *_a, **_k: _frame())
    monkeypatch.setattr(bot.data_feed, "completed_bars", lambda data, _: data)
    monkeypatch.setattr(bot, "add_indicators", lambda data, _: data)
    monkeypatch.setattr(bot, "is_tp_reachable_in_days", lambda *_a, **_k: True)
    monkeypatch.setattr(
        bot.data_feed, "fetch_snapshots",
        lambda symbols: {symbols[0]: {"price": 100.0}},
    )
    monkeypatch.setattr(bot.db_mod, "start_bot_run", lambda *_a: 1)
    monkeypatch.setattr(bot.db_mod, "finish_bot_run", lambda *_a: None)
    monkeypatch.setattr(bot.db_mod, "get_open_trade", lambda *_a: open_trade)
    monkeypatch.setattr(bot.db_mod, "get_open_trades", lambda: [])
    monkeypatch.setattr(bot.db_mod, "save_trade", lambda *_a, **_k: None)
    monkeypatch.setattr(bot.bot_hooks, "log_signal", lambda *_a: None)
    monkeypatch.setattr(bot, "send_notification", lambda *_a, **_k: None)
    monkeypatch.setattr(bot, "_reconcile_and_exit", lambda *_a, **_k: None)

    def place_single(_tc, ticker, qty, _sig, _name, entry_coid=None):
        placed.append((ticker, qty))
        return {"entry_coid": entry_coid or f"coid-{ticker}",
                "alpaca_id": f"id-{ticker}"}

    monkeypatch.setattr(bot, "_place_single_bracket_entry", place_single)
    return placed


def test_open_db_trade_blocks_a_second_entry(monkeypatch):
    """The guard that failed 21 times in the live log (trades 14-34)."""
    placed = _cycle(
        monkeypatch,
        _SharedAccountClient(our_symbols=["NVDA"]),
        ["NVDA"],
        open_trade={"id": 14, "ticker": "NVDA", "strategy": "ensemble"},
    )

    bot.run_once(StrategyType.ENSEMBLE)

    assert placed == []


def test_untracked_broker_position_blocks_entry(monkeypatch):
    """A position the bot did not open must not be stacked on."""
    placed = _cycle(
        monkeypatch,
        _SharedAccountClient(our_symbols=["NVDA"]),
        ["NVDA"],
        open_trade=None,          # DB does not know about it
    )

    bot.run_once(StrategyType.ENSEMBLE)

    assert placed == []


def test_position_lookup_failure_fails_closed(monkeypatch):
    """Only a definitive 404 proves 'no position'; anything else must skip."""
    placed = _cycle(
        monkeypatch,
        _SharedAccountClient(position_error=_LookupFailed("api down")),
        ["NVDA"],
        open_trade=None,
    )

    bot.run_once(StrategyType.ENSEMBLE)

    assert placed == []


def test_clear_ticker_still_enters(monkeypatch):
    """Control: with no DB trade and a clean 404, the entry proceeds."""
    placed = _cycle(
        monkeypatch,
        _SharedAccountClient(foreign_symbols=["SPY"]),
        ["NVDA"],
        open_trade=None,
    )

    bot.run_once(StrategyType.ENSEMBLE)

    assert len(placed) == 1
    assert placed[0][0] == "NVDA"
