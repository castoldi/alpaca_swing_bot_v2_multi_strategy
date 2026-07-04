from datetime import datetime

import bot


class _FixedDateTime:
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 7, 4, 9, 0, tzinfo=tz)


class _Order:
    symbol = "NVDA"
    filled_avg_price = 197.62
    filled_qty = 1
    id = "old-sell-order"
    client_order_id = "b87e551a-ddf5-4b1a-94d2-36a2fffa9807"


class _TradingClientWithOldSell:
    def get_orders(self, filter=None):
        return [_Order()]


def test_trading_hours_excludes_weekends(monkeypatch):
    monkeypatch.setattr(bot, "datetime", _FixedDateTime)

    assert bot._in_trading_hours() is False


def test_reconcile_closed_ignores_unrelated_sell_fills(monkeypatch):
    closed = []
    monkeypatch.setattr(bot.db_mod, "close_trade", lambda *args, **kwargs: closed.append((args, kwargs)))

    trade = {
        "id": 34,
        "ticker": "NVDA",
        "strategy": "ensemble",
        "entry_date": "2026-07-02 16:00:00",
        "entry_price": 194.51,
        "shares": 1,
        "client_order_id": "swingv2-entry-ensemble-NVDA-448facff",
        "alpaca_order_id": "current-entry",
    }

    bot._reconcile_closed(_TradingClientWithOldSell(), trade)

    assert closed == []
