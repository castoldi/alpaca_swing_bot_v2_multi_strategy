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
    assert _configured_strategy_count() == 8


def test_generated_ui_labels_do_not_hardcode_the_old_strategy_count():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert "6 strategies" not in (root / "build_report_2025.py").read_text(encoding="utf-8")
    assert '>6</div><div class="sub">V1 + V2 active' not in (
        root / "dashboard" / "index.html"
    ).read_text(encoding="utf-8")


def test_dashboard_summary_exposes_percentage_sizing(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from dashboard import server

    monkeypatch.setattr(server.db_mod, "portfolio_stats", lambda: {})
    monkeypatch.setattr(server, "_get_trading", lambda: SimpleNamespace())
    monkeypatch.setattr(
        server.db_mod,
        "sync_positions_from_alpaca",
        lambda _client: {"positions": [], "deployed": 0.0},
    )

    summary = asyncio.run(server.get_summary())

    assert summary["position_size_pct"] == 0.20
    assert summary["initial_backtest_equity"] == 1_000.0
    assert summary["max_concurrent_positions"] == 5


def test_dashboard_home_labels_percentage_sizing():
    from pathlib import Path

    html = (
        Path(__file__).resolve().parents[1] / "dashboard" / "index.html"
    ).read_text(encoding="utf-8")

    assert "equity/trade" in html
    assert "dollars_per_trade" not in html
