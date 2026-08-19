"""Broker confirmation must be scoped to this bot's own trades and fail closed."""
import pytest

import broker_sync


class FakePosition:
    def __init__(self, symbol, qty, current_price=None):
        self.symbol = symbol
        self.qty = qty
        self.current_price = current_price


class FakePositionsClient:
    def __init__(self, positions, raises=False):
        self._positions = positions
        self._raises = raises
        self.calls = 0

    def get_all_positions(self):
        self.calls += 1
        if self._raises:
            raise RuntimeError("alpaca down")
        return self._positions


def owned_trade(**kw):
    base = {
        "id": 1,
        "ticker": "NVDA",
        "shares": 10.0,
        "client_order_id": "swingv2-entry-ensemble-NVDA-abc123",
    }
    base.update(kw)
    return base


def test_matching_quantity_is_confirmed():
    tc = FakePositionsClient([FakePosition("NVDA", "10", "222.50")])
    (check,) = broker_sync.check_open_trades(tc, [owned_trade()])
    assert check.status == broker_sync.CONFIRMED
    assert check.broker_shares == pytest.approx(10.0)
    assert check.mark == pytest.approx(222.50)


def test_quantity_difference_is_reported_not_acted_on():
    tc = FakePositionsClient([FakePosition("NVDA", "7", "222.50")])
    (check,) = broker_sync.check_open_trades(tc, [owned_trade(shares=10.0)])
    assert check.status == broker_sync.MISMATCH
    assert "7" in check.detail and "10" in check.detail


def test_absent_position_is_missing():
    tc = FakePositionsClient([])
    (check,) = broker_sync.check_open_trades(tc, [owned_trade()])
    assert check.status == broker_sync.MISSING
    assert check.broker_shares == pytest.approx(0.0)


def test_unreadable_broker_never_reports_missing():
    """An outage proves nothing — reporting `missing` would fabricate a liquidation."""
    tc = FakePositionsClient([], raises=True)
    (check,) = broker_sync.check_open_trades(tc, [owned_trade()])
    assert check.status == broker_sync.UNVERIFIED
    assert check.status != broker_sync.MISSING


def test_a_trade_without_our_prefix_is_never_claimed():
    """Only the swingv2 correlation id proves the bot opened a position."""
    tc = FakePositionsClient([FakePosition("NVDA", "10", "222.50")])
    (check,) = broker_sync.check_open_trades(
        tc, [owned_trade(client_order_id="otherbot-entry-NVDA-1")]
    )
    assert check.status == broker_sync.UNVERIFIED


def test_a_trade_with_no_correlation_id_is_never_claimed():
    tc = FakePositionsClient([FakePosition("NVDA", "10", "222.50")])
    (check,) = broker_sync.check_open_trades(tc, [owned_trade(client_order_id=None)])
    assert check.status == broker_sync.UNVERIFIED


def test_sibling_bot_positions_are_ignored_entirely():
    """Other projects share the key; their symbols must not appear in results."""
    tc = FakePositionsClient([
        FakePosition("NVDA", "10", "222.50"),
        FakePosition("TSLA", "99", "400.00"),   # another bot's position
    ])
    checks = broker_sync.check_open_trades(tc, [owned_trade()])
    assert [c.ticker for c in checks] == ["NVDA"]


def test_negative_broker_quantity_compares_by_magnitude():
    tc = FakePositionsClient([FakePosition("NVDA", "-10", "222.50")])
    (check,) = broker_sync.check_open_trades(tc, [owned_trade(shares=10.0)])
    assert check.status == broker_sync.CONFIRMED


def test_unreadable_quantity_is_unverified_not_a_mismatch():
    tc = FakePositionsClient([FakePosition("NVDA", "not-a-number", "222.50")])
    (check,) = broker_sync.check_open_trades(tc, [owned_trade()])
    assert check.status == broker_sync.UNVERIFIED


def test_empty_account_differs_from_unreadable_account():
    empty = FakePositionsClient([])
    broken = FakePositionsClient([], raises=True)
    assert broker_sync.check_open_trades(empty, [owned_trade()])[0].status == broker_sync.MISSING
    assert broker_sync.check_open_trades(broken, [owned_trade()])[0].status == broker_sync.UNVERIFIED


def test_positions_are_read_once_per_sweep():
    """One broker call regardless of how many trades are checked."""
    tc = FakePositionsClient([FakePosition("NVDA", "10"), FakePosition("AMD", "5")])
    broker_sync.check_open_trades(tc, [
        owned_trade(id=1, ticker="NVDA", shares=10.0),
        owned_trade(id=2, ticker="AMD", shares=5.0),
    ])
    assert tc.calls == 1


def test_marks_and_status_maps_are_snapshot_shaped():
    tc = FakePositionsClient([
        FakePosition("NVDA", "10", "222.50"),
        FakePosition("AMD", "5", None),          # broker could not price it
    ])
    checks = broker_sync.check_open_trades(tc, [
        owned_trade(id=1, ticker="NVDA", shares=10.0),
        owned_trade(id=2, ticker="AMD", shares=5.0),
    ])
    assert broker_sync.marks_from_checks(checks) == {"NVDA": 222.50}
    assert broker_sync.status_map(checks) == {1: "confirmed", 2: "confirmed"}


def test_no_open_trades_makes_no_claims():
    tc = FakePositionsClient([FakePosition("NVDA", "10")])
    assert broker_sync.check_open_trades(tc, []) == []


def test_check_never_places_or_cancels_orders():
    """The sweep is read-only; exits stay in bot.py."""
    class StrictClient(FakePositionsClient):
        def submit_order(self, *a, **k):        # pragma: no cover - must not run
            raise AssertionError("broker_sync must never submit an order")

        def cancel_order_by_id(self, *a, **k):  # pragma: no cover - must not run
            raise AssertionError("broker_sync must never cancel an order")

    tc = StrictClient([FakePosition("NVDA", "3", "222.50")])
    broker_sync.check_open_trades(tc, [owned_trade(shares=10.0)])  # a mismatch
