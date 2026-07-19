"""The one live entry shape: a protected bracket (market buy + OCO TP/SL)."""
import bot
from tests.fakes import FakeTradingClient


def test_single_bracket_entry_submits_one_protected_order():
    tc = FakeTradingClient()

    class Sig:
        stop_loss = 90.0
        tp3 = 110.0

    info = bot._place_single_bracket_entry(tc, "AMD", 5, Sig(), "ensemble")

    assert len(tc.submitted) == 1
    order = tc.submitted[0]
    assert order.order_class == "bracket"
    assert order.qty == 5
    assert order.stop_loss.stop_price == 90.0
    assert order.take_profit.limit_price == 110.0
    assert info["entry_coid"].startswith("swingv2-entry-ensemble-AMD-")
    assert info["alpaca_id"] == tc.submitted[0].id


def test_set_entry_fill_updates_only_open_trades(tmp_path, monkeypatch):
    from dashboard import db as db_mod

    monkeypatch.setattr(db_mod, "_DB", tmp_path / "test.db")
    db_mod.init_db()
    trade_id = db_mod.save_trade(
        "AMD", "ensemble", "2026-07-18", 100.0, 90.0, 110.0, shares=5,
        client_order_id="swingv2-entry-ensemble-AMD-t1",
    )

    db_mod.set_entry_fill(trade_id, 100.42, 5.0)
    trade = db_mod.get_open_trade("AMD", "ensemble")
    assert trade["entry_filled_price"] == 100.42
    assert trade["shares"] == 5.0

    db_mod.close_trade(trade_id, "2026-07-19", 105.0, "take_profit", 1, 5, 22.9, 0.0458)
    db_mod.set_entry_fill(trade_id, 999.0)
    closed = db_mod.get_all_trades(limit=1)[0]
    assert closed["entry_filled_price"] == 100.42
