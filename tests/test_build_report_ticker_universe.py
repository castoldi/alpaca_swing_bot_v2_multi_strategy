"""Regression: per-ticker report charts must not assume every strategy shares
the same ticker universe.

`build_report_2025` used to source each ticker's price chart from whichever
strategy came first in `per_strategy_details`' dict order, regardless of
whether that strategy actually traded the ticker. Once strategies gained their
own scoped universes (tqqq_momentum -> TQQQ only), the first strategy in
insertion order frequently had no data at all for a given ticker, producing an
empty DataFrame with no "close" column and crashing `add_indicators` inside
`ticker_chart` with `KeyError: 'close'`.

Reproduced on unmodified `main` via `git stash` before fixing, so this is not
speculative — it broke every real `backtest_2024/2025/2026.py` run.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backtest_2025 import compute_stats
from build_report_2025 import build_report_2025, ticker_chart
from strategies.base import Trade


def _frame(periods=60):
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


def _trade(ticker="NVDA", strategy="ensemble"):
    return Trade(
        ticker=ticker, entry_date=pd.Timestamp("2026-01-05"), entry_price=100.0,
        stop_loss=90.0, take_profit=110.0,
        exit_date=pd.Timestamp("2026-01-06"), exit_price=105.0,
        exit_reason="take_profit", bars_held=1, shares=1,
        pnl_dollars=5.0, pnl_pct=0.05, strategy=strategy,
    )


def test_ticker_chart_handles_an_empty_frame_without_crashing():
    html = ticker_chart("NVDA", pd.DataFrame(), [])
    assert "No price data available" in html
    assert "NVDA" in html


def _stats(trades):
    return compute_stats(trades)


def test_report_uses_a_strategy_that_actually_traded_the_ticker():
    # "aaa_never_trades_nvda" sorts first but has no NVDA data, matching the
    # real scenario: tqqq_momentum sorts/iterates before other strategies in
    # some registries and only ever has TQQQ.
    per_strategy_details = {
        "aaa_never_trades_nvda": {},
        "ensemble": {"NVDA": (_frame(), [_trade()])},
    }
    strategy_results = {
        "aaa_never_trades_nvda": _stats([]),
        "ensemble": _stats([_trade()]),
    }

    html = build_report_2025(strategy_results, per_strategy_details, "ensemble")

    assert "No price data available" not in html
    assert "NVDA" in html


def test_report_falls_back_gracefully_when_no_strategy_has_the_ticker():
    # A ticker with trades recorded but no strategy retained its price frame
    # (shouldn't happen in practice, but must degrade, not crash).
    per_strategy_details = {"ensemble": {"NVDA": (pd.DataFrame(), [_trade()])}}
    strategy_results = {"ensemble": _stats([_trade()])}

    html = build_report_2025(strategy_results, per_strategy_details, "ensemble")

    assert "No price data available for NVDA" in html


def test_report_builds_across_the_full_ticker_universe_without_keyerror():
    # ALL_TICKERS includes TQQQ; only tqqq_momentum ever trades it, and it is
    # NOT the first key here - exercises the exact original crash path.
    per_strategy_details = {
        "ensemble": {
            "NVDA": (_frame(), [_trade("NVDA")]),
            "AMZN": (_frame(), []),
        },
        "tqqq_momentum": {"TQQQ": (_frame(), [_trade("TQQQ", "tqqq_momentum")])},
    }
    strategy_results = {
        "ensemble": _stats([_trade("NVDA")]),
        "tqqq_momentum": _stats([_trade("TQQQ", "tqqq_momentum")]),
    }

    html = build_report_2025(strategy_results, per_strategy_details, "ensemble")

    assert "TQQQ" in html
    assert "No price data available" not in html
