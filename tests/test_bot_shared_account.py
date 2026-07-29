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


# ── External liquidation by a sibling project ─────────────────────────────────
#
# Trades 36-40: a sibling bot on the same key ran account-wide
# close_all_positions(). Our brackets were canceled with 0 fills and the shares
# were market-sold by orders this bot never placed. `_confirmed_exit_fill`
# rightly refuses to call a foreign sell our exit, so the rows stayed
# status='open' forever and burned 4 of 5 position slots. These pin down the
# post-mortem path that closes them WITHOUT loosening ownership anywhere else.

def _order(coid, *, qty, price, submitted, status="filled", side="sell"):
    return SimpleNamespace(
        id=f"oid-{coid}",
        client_order_id=coid,
        symbol="NVDA",
        side=SimpleNamespace(value=side),
        status=SimpleNamespace(value=status),
        filled_qty=str(qty),
        filled_avg_price=(None if price is None else str(price)),
        filled_at=submitted,
        submitted_at=submitted,
        updated_at=submitted,
        legs=None,
    )


class _LiquidatedClient:
    """The real post-flatten shape: our entry filled, its bracket legs were
    canceled with 0 fills, and the supplied sell orders exist at the broker.

    Reading our own entry order back matters — the legs hanging off it are how
    a genuine bracket fill is recognised as ours despite its broker-generated
    client id. `entry_readable=False` simulates the API failing that lookup.
    """

    def __init__(self, sells, entry_readable=True):
        self.sells = list(sells)
        self.entry_readable = entry_readable

    def _entry(self):
        canceled_leg = _order("cea37086-canceled-stop", qty=97, price=None,
                              submitted="2026-07-21T16:28:39+00:00", status="canceled")
        canceled_leg.filled_qty = "0"
        return SimpleNamespace(
            id="entry-oid",
            client_order_id="swingv2-entry-ensemble-NVDA-18927007",
            symbol="NVDA",
            side=SimpleNamespace(value="buy"),
            status=SimpleNamespace(value="filled"),
            filled_qty="97",
            filled_avg_price="205.19",
            legs=[canceled_leg],
        )

    def get_orders(self, filter=None):
        want_closed = str(getattr(getattr(filter, "status", None), "value", "")) == "closed"
        return list(self.sells) if want_closed else []

    def get_order_by_id(self, _order_id, filter=None):
        if not self.entry_readable:
            raise RuntimeError("api unavailable")
        return self._entry()

    def get_order_by_client_id(self, _coid):
        if not self.entry_readable:
            raise RuntimeError("api unavailable")
        return self._entry()


@pytest.fixture
def liquidation_db(monkeypatch):
    """Capture close_trade instead of touching the real SQLite file."""
    state = {"closed": [], "used": set(), "progress": []}

    def record(trade_id, order_id, coid, shares, notional):
        state["progress"].append((trade_id, order_id, shares, notional))

    def totals(trade_id):
        rows = [p for p in state["progress"] if p[0] == trade_id]
        return sum(r[2] for r in rows), sum(r[3] for r in rows)

    def close(db_id, exit_date, exit_price, reason, bars, shares, pnl, pnl_pct, **kw):
        state["closed"].append(
            {"id": db_id, "price": exit_price, "reason": reason,
             "shares": shares, "pnl": pnl}
        )

    monkeypatch.setattr(bot.db_mod, "record_exit_order_progress", record)
    monkeypatch.setattr(bot.db_mod, "get_exit_fill_totals", totals)
    monkeypatch.setattr(bot.db_mod, "close_trade", close)
    monkeypatch.setattr(
        bot.db_mod, "exit_order_already_used", lambda oid: oid in state["used"]
    )
    monkeypatch.setattr(bot, "send_notification", lambda *_a, **_k: None)
    return state


def _trade(shares=97.0):
    return {
        "id": 38,
        "ticker": "NVDA",
        "strategy": "ensemble",
        "entry_date": "2026-07-21 12:00:00",
        "created_at": "2026-07-21T16:28:39+00:00",
        "entry_price": 205.77,
        "entry_filled_price": 205.19,
        "shares": shares,
        "client_order_id": "swingv2-entry-ensemble-NVDA-18927007",
        "alpaca_order_id": "entry-oid",
    }


def test_foreign_liquidation_closes_the_stuck_trade(liquidation_db):
    """The real trade-38 shape: full-size foreign sell right after our entry."""
    client = _LiquidatedClient([
        _order("77631560-453c-48e2", qty=97, price=205.14,
               submitted="2026-07-21T16:28:41+00:00"),
    ])

    assert bot._reconcile_closed(client, _trade()) is True

    closed = liquidation_db["closed"]
    assert len(closed) == 1
    assert closed[0]["reason"] == "external_liquidation"
    assert closed[0]["price"] == pytest.approx(205.14)
    # 97 x (205.14 - 205.19), the loss the flatten actually cost us.
    assert closed[0]["pnl"] == pytest.approx(-4.85, abs=0.01)


def test_our_own_sell_is_never_treated_as_foreign(liquidation_db):
    """Ownership is not loosened: a prefixed fill exits by the normal path."""
    client = _LiquidatedClient([
        _order("swingv2-exit-ensemble-NVDA-abc", qty=97, price=210.0,
               submitted="2026-07-21T18:00:00+00:00"),
    ])

    assert bot._reconcile_closed(client, _trade()) is True

    # Our own order id -> a real strategy reason, not the liquidation label.
    assert liquidation_db["closed"][0]["reason"] != "external_liquidation"


def test_foreign_sell_before_our_entry_is_ignored(liquidation_db):
    """An older unrelated sell must not be back-attributed to this trade."""
    client = _LiquidatedClient([
        _order("stale-order", qty=97, price=190.0,
               submitted="2026-07-01T15:00:00+00:00"),
    ])

    assert bot._reconcile_closed(client, _trade()) is False
    assert liquidation_db["closed"] == []


def test_foreign_sell_smaller_than_our_position_is_ignored(liquidation_db):
    """A 10-share foreign sell cannot explain our 97 shares disappearing."""
    client = _LiquidatedClient([
        _order("someone-else", qty=10, price=205.0,
               submitted="2026-07-21T16:28:41+00:00"),
    ])

    assert bot._reconcile_closed(client, _trade()) is False
    assert liquidation_db["closed"] == []


def test_aggregated_flatten_credits_only_our_shares(liquidation_db):
    """Two bots held NVDA; the flatten sold 150. We own 97 of that P&L, not 150."""
    client = _LiquidatedClient([
        _order("aggregate-flatten", qty=150, price=205.14,
               submitted="2026-07-21T16:28:41+00:00"),
    ])

    assert bot._reconcile_closed(client, _trade()) is True

    assert liquidation_db["closed"][0]["shares"] == pytest.approx(97.0)
    assert liquidation_db["closed"][0]["pnl"] == pytest.approx(-4.85, abs=0.01)


def test_fill_already_claimed_by_another_trade_is_not_reused(liquidation_db):
    """One broker fill closes exactly one DB trade."""
    liquidation_db["used"].add("oid-aggregate-flatten")
    client = _LiquidatedClient([
        _order("aggregate-flatten", qty=150, price=205.14,
               submitted="2026-07-21T16:28:41+00:00"),
    ])

    assert bot._reconcile_closed(client, _trade()) is False
    assert liquidation_db["closed"] == []


def test_earliest_qualifying_foreign_fill_wins(liquidation_db):
    """The first flatten took the position; later sells are someone else's."""
    client = _LiquidatedClient([
        _order("later", qty=97, price=250.0,
               submitted="2026-07-23T16:00:00+00:00"),
        _order("the-flatten", qty=97, price=205.14,
               submitted="2026-07-21T16:28:41+00:00"),
    ])

    assert bot._reconcile_closed(client, _trade()) is True
    assert liquidation_db["closed"][0]["price"] == pytest.approx(205.14)


def test_unreadable_own_orders_blocks_foreign_attribution(liquidation_db):
    """Fail closed: our own bracket legs also carry broker client ids, so with
    the parent order unreadable a real stop fill is indistinguishable from a
    foreign flatten. Leaving the row open beats mislabeling it."""
    client = _LiquidatedClient(
        [_order("could-be-our-own-stop-leg", qty=97, price=205.14,
                submitted="2026-07-21T16:28:41+00:00")],
        entry_readable=False,
    )

    assert bot._reconcile_closed(client, _trade()) is False
    assert liquidation_db["closed"] == []
