import pandas as pd
import bot
from tests.fakes import FakeTradingClient
from strategy import EntrySignal


def _sig():
    return EntrySignal(date=pd.Timestamp("2026-01-02"), entry_price=100.0,
                       stop_loss=90.0, take_profit=108.0, atr=2.0, rsi=55.0,
                       strategy="ensemble")


def test_place_scaled_entry_orders():
    tc = FakeTradingClient()
    bot._place_scaled_entry(tc, "AMD", qty=9, sig=_sig(), strat_name="ensemble")
    types = [o.type for o in tc.submitted]
    # 1 market buy + 3 limit sells + 1 stop sell
    assert types.count("MarketOrderRequest") == 1
    assert types.count("LimitOrderRequest") == 3
    assert types.count("StopOrderRequest") == 1
    sells = [o for o in tc.submitted if o.type == "LimitOrderRequest"]
    assert sorted(int(o.qty) for o in sells) == [3, 3, 3]
    # limit prices are tp1/tp2/tp3
    assert sorted(round(o.limit_price, 2) for o in sells) == [102.67, 105.33, 108.0]
    stop = [o for o in tc.submitted if o.type == "StopOrderRequest"][0]
    assert int(stop.qty) == 9 and stop.stop_price == 90.0
    # every order is bot-owned
    assert all(o.client_order_id and o.client_order_id.startswith("swingv2") for o in tc.submitted)
