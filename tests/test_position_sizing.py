import math

from position_sizing import whole_share_position_size


def test_sizes_twenty_percent_of_equity_in_whole_shares():
    result = whole_share_position_size(1_000.0, 1_000.0, 60.0, 0.20)

    assert result.quantity == 3
    assert result.budget == 200.0
    assert result.notional == 180.0
    assert result.reason is None


def test_caps_order_by_available_cash():
    result = whole_share_position_size(2_000.0, 150.0, 60.0, 0.20)

    assert result.quantity == 2
    assert result.budget == 150.0
    assert result.notional == 120.0
    assert result.reason is None


def test_skips_price_above_allocation():
    result = whole_share_position_size(1_000.0, 1_000.0, 250.0, 0.20)

    assert result.quantity == 0
    assert result.reason == "budget_below_one_share"


def test_rejects_invalid_numeric_inputs():
    cases = [
        (math.nan, 1_000.0, 100.0, 0.20),
        (1_000.0, -1.0, 100.0, 0.20),
        (1_000.0, 1_000.0, 0.0, 0.20),
        (1_000.0, 1_000.0, 100.0, 0.0),
        (1_000.0, 1_000.0, 100.0, 1.01),
    ]

    for args in cases:
        result = whole_share_position_size(*args)
        assert result.quantity == 0
        assert result.reason == "invalid_input"
