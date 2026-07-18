from strategies import REGISTRY


def test_dashboard_strategy_metadata_contains_sma_cross():
    meta = REGISTRY["sma_50_cross"].meta()
    assert meta["label"] == "SMA 50 Cross"
    assert meta["timeframe"] == "1d"
    assert meta["has_take_profit"] is False


def test_sma_cross_example_target_is_absent():
    from dashboard.strategy_examples import _target_value

    assert _target_value(REGISTRY["sma_50_cross"], 0.0) is None
