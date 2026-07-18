"""Minimal stand-in for alpaca TradingClient — records submitted orders, no network."""
import itertools


class FakeOrder:
    _ids = itertools.count(1)

    def __init__(self, req):
        self.id = f"ord-{next(self._ids)}"
        self.client_order_id = getattr(req, "client_order_id", None)
        self.symbol = getattr(req, "symbol", None)
        self.qty = getattr(req, "qty", None)
        self.side = getattr(req, "side", None)
        self.limit_price = getattr(req, "limit_price", None)
        self.stop_price = getattr(req, "stop_price", None)
        order_class = getattr(req, "order_class", None)
        self.order_class = getattr(order_class, "value", order_class)
        self.stop_loss = getattr(req, "stop_loss", None)
        self.take_profit = getattr(req, "take_profit", None)
        self.type = type(req).__name__
        self.status = "new"
        self.legs = []


class FakeTradingClient:
    def __init__(self):
        self.submitted = []
        self.cancelled = []

    def submit_order(self, req):
        o = FakeOrder(req)
        self.submitted.append(o)
        return o

    def cancel_order_by_id(self, oid):
        self.cancelled.append(oid)
