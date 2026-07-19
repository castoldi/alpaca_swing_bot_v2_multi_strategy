from __future__ import annotations

import pandas as pd
import pytest

from backtest_portfolio import (
    BacktestCandidate,
    collect_backtest_candidates,
    materialize_candidate,
    run_annual_portfolio,
)
from config import PARAMS
from strategies.base import BaseStrategy, EntrySignal, ExitLeg


def _candidate(*, scaled_legs: tuple[ExitLeg, ...] | None = None):
    entry_date = pd.Timestamp("2026-01-02")
    single = (
        ExitLeg(pd.Timestamp("2026-01-05"), 110.0, "take_profit", 1, 1.0),
    )
    scaled = scaled_legs or (
        ExitLeg(pd.Timestamp("2026-01-03"), 103.0, "tp1", 1, 0.33),
        ExitLeg(pd.Timestamp("2026-01-04"), 106.0, "tp2", 2, 0.33),
        ExitLeg(pd.Timestamp("2026-01-05"), 110.0, "tp3", 3, 0.34),
    )
    return BacktestCandidate(
        ticker="TEST",
        entry_date=entry_date,
        entry_price=100.0,
        stop_loss=90.0,
        take_profit=110.0,
        strategy="trend_pullback",
        single_legs=single,
        scaled_legs=scaled,
    )


def test_small_quantities_use_single_bracket_outcome():
    trades = materialize_candidate(_candidate(), 2)

    assert [trade.shares for trade in trades] == [2]
    assert [trade.exit_reason for trade in trades] == ["take_profit"]


def test_large_quantities_also_use_the_single_bracket_outcome():
    # Live places one protected bracket per entry regardless of quantity
    # (Alpaca rejects extra concurrent sell legs), so the backtest must too.
    trades = materialize_candidate(_candidate(), 7)

    assert [trade.shares for trade in trades] == [7]
    assert [trade.exit_reason for trade in trades] == ["take_profit"]


def test_scaled_legs_are_never_materialized():
    scaled = (
        ExitLeg(pd.Timestamp("2026-01-03"), 103.0, "tp1", 1, 0.33),
        ExitLeg(pd.Timestamp("2026-01-04"), 100.0, "stop_loss", 2, 0.67),
    )

    trades = materialize_candidate(_candidate(scaled_legs=scaled), 7)

    assert [trade.exit_reason for trade in trades] == ["take_profit"]


class _RepeatedSignalStrategy(BaseStrategy):
    name = "test_repeated"

    def __init__(self, signal_indexes):
        super().__init__()
        self.signal_indexes = set(signal_indexes)

    def check_entry(self, df, idx, p=PARAMS):
        if idx not in self.signal_indexes:
            return None
        return EntrySignal(
            date=pd.Timestamp(df.index[idx]),
            entry_price=float(df.iloc[idx]["close"]),
            stop_loss=90.0,
            take_profit=110.0,
            atr=10.0,
            rsi=55.0,
            strategy=self.name,
        )


def _bars(periods=8):
    index = pd.date_range("2026-01-01", periods=periods, freq="D")
    return pd.DataFrame(
        {
            "open": [100.0] * periods,
            "high": [101.0] * periods,
            "low": [99.0] * periods,
            "close": [100.0] * periods,
            "volume": [1_000_000.0] * periods,
        },
        index=index,
    )


def test_candidate_collection_keeps_repeated_signals_for_portfolio_filtering():
    frame = _bars()
    strategy = _RepeatedSignalStrategy({1, 2})

    candidates = collect_backtest_candidates(
        frame,
        "TEST",
        frame.index[0],
        frame.index[-1],
        PARAMS,
        strategy,
    )

    assert [candidate.entry_date for candidate in candidates] == [
        frame.index[1],
        frame.index[2],
    ]


def test_candidate_exit_is_clipped_to_annual_end():
    frame = _bars()
    strategy = _RepeatedSignalStrategy({2})

    candidates = collect_backtest_candidates(
        frame,
        "TEST",
        frame.index[0],
        frame.index[4],
        PARAMS,
        strategy,
    )

    assert candidates[0].single_legs[-1].exit_date == frame.index[4]
    assert candidates[0].scaled_legs[-1].exit_date == frame.index[4]


def _single_candidate(
    ticker,
    entry_date,
    entry_price,
    exit_date,
    exit_price,
):
    reason = "take_profit" if exit_price >= entry_price else "stop_loss"
    leg = ExitLeg(
        pd.Timestamp(exit_date),
        float(exit_price),
        reason,
        1,
        1.0,
    )
    return BacktestCandidate(
        ticker=ticker,
        entry_date=pd.Timestamp(entry_date),
        entry_price=float(entry_price),
        stop_loss=float(entry_price) * 0.9,
        take_profit=float(exit_price),
        strategy="test",
        single_legs=(leg,),
        scaled_legs=(leg,),
    )


def test_profit_increases_a_later_whole_share_allocation():
    candidates = [
        _single_candidate("A", "2026-01-02", 51, "2026-01-03", 85),
        _single_candidate("B", "2026-01-04", 51, "2026-01-05", 51),
    ]

    result = run_annual_portfolio(
        candidates, initial_equity=1_000.0, position_fraction=0.20,
        max_positions=5,
    )

    assert [trade.shares for trade in result.trades] == [3, 4]
    assert result.ending_equity == 1_102.0


def test_loss_decreases_a_later_whole_share_allocation():
    candidates = [
        _single_candidate("A", "2026-01-02", 49, "2026-01-03", 24),
        _single_candidate("B", "2026-01-04", 49, "2026-01-05", 49),
    ]

    result = run_annual_portfolio(
        candidates, initial_equity=1_000.0, position_fraction=0.20,
        max_positions=5,
    )

    assert [trade.shares for trade in result.trades] == [4, 3]
    assert result.ending_equity == 900.0


def test_sixth_overlapping_position_is_skipped():
    candidates = [
        _single_candidate(
            ticker, f"2026-01-0{index + 2}", 100,
            "2026-01-20", 100,
        )
        for index, ticker in enumerate("ABCDEF")
    ]

    result = run_annual_portfolio(
        candidates, initial_equity=1_000.0, position_fraction=0.20,
        max_positions=5,
    )

    assert result.accepted_positions == 5
    assert result.skipped_positions == 1


def test_scaleout_legs_consume_one_position_slot():
    scaled = _candidate()
    scaled = BacktestCandidate(
        ticker="A",
        entry_date=scaled.entry_date,
        entry_price=60.0,
        stop_loss=54.0,
        take_profit=70.0,
        strategy=scaled.strategy,
        single_legs=scaled.single_legs,
        scaled_legs=scaled.scaled_legs,
    )
    second = _single_candidate("B", "2026-01-03", 100, "2026-01-06", 100)

    result = run_annual_portfolio(
        [scaled, second], initial_equity=1_000.0, position_fraction=0.20,
        max_positions=2,
    )

    assert result.accepted_positions == 2


def test_available_cash_caps_entries_before_position_limit():
    candidates = [
        _single_candidate("A", "2026-01-02", 100, "2026-01-20", 100),
        _single_candidate("B", "2026-01-03", 60, "2026-01-20", 60),
        _single_candidate("C", "2026-01-04", 30, "2026-01-20", 30),
    ]

    result = run_annual_portfolio(
        candidates, initial_equity=1_000.0, position_fraction=0.80,
        max_positions=5,
    )

    assert [trade.shares for trade in result.trades] == [8, 3]
    assert result.accepted_positions == 2
    assert result.skipped_positions == 1


def test_second_candidate_for_open_ticker_is_skipped():
    candidates = [
        _single_candidate("A", "2026-01-02", 100, "2026-01-10", 100),
        _single_candidate("A", "2026-01-03", 100, "2026-01-04", 110),
    ]

    result = run_annual_portfolio(
        candidates, initial_equity=1_000.0, position_fraction=0.20,
        max_positions=5,
    )

    assert result.accepted_positions == 1
    assert result.skipped_positions == 1
    assert result.ending_equity == 1_000.0


def test_ending_equity_equals_start_plus_realized_pnl():
    candidates = [
        _single_candidate("A", "2026-01-02", 100, "2026-01-03", 110),
        _single_candidate("B", "2026-01-04", 100, "2026-01-05", 95),
    ]

    result = run_annual_portfolio(
        candidates, initial_equity=1_000.0, position_fraction=0.20,
        max_positions=5,
    )

    assert result.ending_equity == 1_000.0 + sum(
        trade.pnl_dollars for trade in result.trades
    )
    assert result.return_pct == (
        result.ending_equity - result.starting_equity
    ) / result.starting_equity


def test_same_timestamp_entries_use_the_same_pre_event_equity():
    candidates = [
        _single_candidate("A", "2026-01-02", 100, "2026-01-02", 50),
        _single_candidate("B", "2026-01-02", 100, "2026-01-03", 100),
    ]

    result = run_annual_portfolio(
        candidates,
        initial_equity=1_000.0,
        position_fraction=0.20,
        max_positions=5,
    )

    assert [trade.shares for trade in result.trades] == [2, 2]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"initial_equity": 0.0, "position_fraction": 0.20, "max_positions": 5},
        {"initial_equity": 1_000.0, "position_fraction": 0.0, "max_positions": 5},
        {"initial_equity": 1_000.0, "position_fraction": 0.20, "max_positions": 0},
    ],
)
def test_annual_portfolio_rejects_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        run_annual_portfolio([], **kwargs)


def test_strategies_package_exports_candidate_api():
    from strategies import (
        BacktestCandidate as ExportedCandidate,
        collect_backtest_candidates as exported_collect,
        materialize_candidate as exported_materialize,
    )

    assert ExportedCandidate is BacktestCandidate
    assert exported_collect is collect_backtest_candidates
    assert exported_materialize is materialize_candidate
