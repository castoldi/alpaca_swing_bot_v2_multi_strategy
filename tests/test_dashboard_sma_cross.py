from strategies import REGISTRY


def test_dashboard_strategy_metadata_contains_sma_cross():
    meta = REGISTRY["sma_50_cross"].meta()
    assert meta["label"] == "SMA 50 Cross"
    assert meta["timeframe"] == "1d"
    assert meta["has_take_profit"] is False


def test_sma_cross_example_target_is_absent():
    from dashboard.strategy_examples import _target_value

    assert _target_value(REGISTRY["sma_50_cross"], 0.0) is None


def test_dashboard_summary_reports_every_configured_timeframe():
    from dashboard.server import _configured_strategy_count, _configured_timeframes

    assert _configured_timeframes() == ["4h", "1d"]
    assert _configured_strategy_count() == 7


def test_generated_ui_labels_do_not_hardcode_the_old_strategy_count():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert "6 strategies" not in (root / "build_report_2025.py").read_text(encoding="utf-8")
    assert '>6</div><div class="sub">V1 + V2 active' not in (
        root / "dashboard" / "index.html"
    ).read_text(encoding="utf-8")
