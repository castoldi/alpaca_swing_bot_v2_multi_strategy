from dashboard import db


def test_durable_entry_intent_can_attach_broker_id_after_acceptance(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(db, "_DB", tmp_path / "entry-intent.db")
    db._ensure_tables()
    trade_id = db.save_trade(
        "AMD",
        "ensemble",
        "2026-07-18",
        100.0,
        90.0,
        110.0,
        shares=2,
        client_order_id="swingv2-entry-ensemble-AMD-abcd",
        alpaca_order_id=None,
        entry_state="pending_submission",
    )

    pending = db.get_open_trade("AMD", "ensemble")
    assert pending["entry_state"] == "pending_submission"

    db.set_entry_order_id(trade_id, "entry-42")

    trade = db.get_open_trade("AMD", "ensemble")
    assert trade["client_order_id"] == "swingv2-entry-ensemble-AMD-abcd"
    assert trade["alpaca_order_id"] == "entry-42"
    assert trade["entry_state"] == "accepted"


def test_pending_exit_remains_open_until_confirmed_fill(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "_DB", tmp_path / "trades.db")
    db._ensure_tables()
    trade_id = db.save_trade(
        "AMD",
        "sma_50_cross",
        "2026-07-01",
        100.0,
        90.0,
        0.0,
        shares=1,
        client_order_id="swingv2-entry-sma_50_cross-AMD-abcd",
        alpaca_order_id="entry-1",
    )

    db.set_exit_intent(
        trade_id,
        "sma_cross_down",
        "swingv2-exit-sma_50_cross-AMD-efgh",
    )
    db.set_exit_pending(
        trade_id,
        "swingv2-exit-sma_50_cross-AMD-efgh",
        "exit-1",
    )
    pending = db.get_open_trade("AMD", "sma_50_cross")
    assert pending["exit_intent_reason"] == "sma_cross_down"
    assert pending["exit_alpaca_order_id"] == "exit-1"
    assert db.exit_order_already_used("exit-1") is False

    db.record_exit_order_progress(
        trade_id,
        "stop-1",
        "broker-stop",
        0.5,
        45.0,
    )
    db.record_exit_order_progress(
        trade_id,
        "stop-1",
        "broker-stop",
        0.5,
        45.0,
    )
    db.record_exit_order_progress(
        trade_id,
        "exit-1",
        "swingv2-exit-sma_50_cross-AMD-efgh",
        0.5,
        49.25,
    )
    assert db.get_exit_fill_totals(trade_id) == (1.0, 94.25)

    db.close_trade(
        trade_id,
        "2026-07-18T14:30:00+00:00",
        98.5,
        "sma_cross_down",
        17,
        1,
        -1.5,
        -0.015,
        exit_client_order_id="swingv2-exit-sma_50_cross-AMD-efgh",
        exit_alpaca_order_id="exit-1",
    )
    assert db.get_open_trade("AMD", "sma_50_cross") is None
    assert db.exit_order_already_used("exit-1") is True
