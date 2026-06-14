import bot
from tests.fakes import FakeTradingClient, FakeOrder


class _Req:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_sync_moves_stop_to_breakeven_after_one_tp(monkeypatch):
    tc = FakeTradingClient()
    # current open stop at initial SL, qty 6 remaining (one TP of 3 already filled)
    stop = FakeOrder(_Req(symbol="AMD", qty=6, side="sell", stop_price=90.0,
                          client_order_id="swingv2-stop-ensemble-AMD-1"))
    stop.type = "StopOrderRequest"
    monkeypatch.setattr(bot, "_open_stop_order", lambda tc_, t: stop)
    monkeypatch.setattr(bot, "_count_filled_tp_legs", lambda tc_, t: 1)
    monkeypatch.setattr(bot, "_position_qty", lambda tc_, tk: 6.0)

    trade = {"ticker": "AMD", "strategy": "ensemble", "entry_price": 100.0,
             "stop_loss": 90.0, "take_profit": 108.0}
    bot._sync_stepped_stop(tc, trade)

    # old stop cancelled, new stop placed at breakeven (100.0) for qty 6
    assert tc.cancelled == [stop.id]
    new = [o for o in tc.submitted if o.type == "StopOrderRequest"][-1]
    assert new.stop_price == 100.0 and int(new.qty) == 6
