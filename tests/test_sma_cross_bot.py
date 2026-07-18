from types import SimpleNamespace

import pandas as pd

import bot
from config import PARAMS
from strategies.base import add_indicators
from tests.fakes import FakeTradingClient


def test_place_stop_only_entry_submits_one_oto_with_no_take_profit():
    tc = FakeTradingClient()
    info = bot._place_stop_only_entry(tc, "AMD", 2, 90.0, "sma_50_cross")
    assert len(tc.submitted) == 1
    order = tc.submitted[0]
    assert order.order_class == "oto"
    assert order.stop_loss.stop_price == 90.0
    assert order.take_profit is None
    assert info["entry_coid"].startswith("swingv2-entry-sma_50_cross-AMD-")


def test_reconcile_closes_owned_sma_trade_on_cross_down(monkeypatch):
    closes = [100.0] * 49 + [101.0, 99.0]
    values = pd.Series(
        closes, index=pd.date_range("2026-01-01", periods=len(closes), freq="D")
    )
    frame = add_indicators(pd.DataFrame({
        "open": values,
        "high": values + 1,
        "low": values - 1,
        "close": values,
        "volume": 1_000_000,
    }), PARAMS)
    trade = {
        "id": 1,
        "ticker": "AMD",
        "strategy": "sma_50_cross",
        "entry_price": 100.0,
        "stop_loss": 90.0,
        "take_profit": 0.0,
        "shares": 1,
        "client_order_id": "swingv2-entry-sma_50_cross-AMD-abcd",
        "alpaca_order_id": "entry-1",
        "entry_date": "2026-07-01",
    }
    tc = SimpleNamespace(
        get_open_position=lambda _: SimpleNamespace(qty="1", current_price="99")
    )
    monkeypatch.setattr(bot, "_get_trading", lambda: tc)
    monkeypatch.setattr(
        bot.db_mod, "get_open_trades_by_strategy", lambda _: [trade]
    )
    monkeypatch.setattr(bot, "_verify_owned", lambda *_: True)
    closed = []
    monkeypatch.setattr(
        bot, "_close_owned",
        lambda *args, **kwargs: closed.append(kwargs["reason"]),
    )
    bot._reconcile_and_exit("sma_50_cross", {"AMD": frame})
    assert closed == ["sma_cross_down"]
