"""TQQQ momentum strategy — entry/exit rules, scoping, and engine wiring."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from config import PARAMS, TICKERS, LEVERAGED_TICKERS, ALL_TICKERS
from strategies import REGISTRY, get_strategy
from strategies.base import add_indicators, tsi


WARMUP = PARAMS.tqqq_ema_period + PARAMS.tqqq_tsi_long + PARAMS.tqqq_tsi_short


def _frame(closes: list[float]) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=len(closes), freq="4h")
    close = pd.Series(closes, index=index)
    return add_indicators(
        pd.DataFrame(
            {
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": [1_000_000.0] * len(closes),
            },
            index=index,
        ),
        PARAMS,
    )


def _ramp_then(turn: float, n_up: int = 260, n_after: int = 60) -> list[float]:
    """Steady uptrend, then a move at `turn` percent per bar."""
    prices = [100.0 * (1.004 ** i) for i in range(n_up)]
    last = prices[-1]
    prices += [last * (turn ** (i + 1)) for i in range(n_after)]
    return prices


def _walk(n: int = 400, drift: float = 0.0015, vol: float = 0.02,
          seed: int = 7) -> list[float]:
    """Seeded random walk. A perfectly smooth curve has monotonic momentum and
    so never produces a TSI crossing — crossings need real noise."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(drift, vol, n)
    return (100.0 * np.cumprod(1.0 + steps)).tolist()


# ── indicator ────────────────────────────────────────────────────────────────

def test_tsi_is_bounded_and_tracks_direction():
    up = pd.Series([100.0 * (1.01 ** i) for i in range(200)])
    line, signal = tsi(up, 25, 13, 13)
    assert line.dropna().between(-100.001, 100.001).all()
    # A pure uptrend drives TSI strongly positive.
    assert line.iloc[-1] > 50
    assert np.isfinite(signal.iloc[-1])


def test_add_indicators_exposes_tsi_columns():
    df = _frame([100.0 + i for i in range(120)])
    for col in ("tsi", "tsi_signal", "ema_trend"):
        assert col in df.columns


# ── entry ────────────────────────────────────────────────────────────────────

def test_no_entry_before_warmup():
    strat = get_strategy("tqqq_momentum")
    df = _frame([100.0 + i for i in range(WARMUP + 5)])
    assert strat.check_entry(df, WARMUP - 1, PARAMS) is None


def test_entry_fires_on_tsi_cross_up():
    strat = get_strategy("tqqq_momentum")
    df = _frame(_walk())

    crossings = [
        i for i in range(WARMUP, len(df))
        if strat.check_entry(df, i, PARAMS) is not None
    ]
    assert crossings, "expected at least one TSI cross-up entry"

    idx = crossings[0]
    prev, row = df.iloc[idx - 1], df.iloc[idx]
    assert prev["tsi"] <= prev["tsi_signal"]
    assert row["tsi"] > row["tsi_signal"]


def test_entry_signal_has_stop_and_no_take_profit():
    strat = get_strategy("tqqq_momentum")
    df = _frame(_walk())
    sig = next(
        strat.check_entry(df, i, PARAMS)
        for i in range(WARMUP, len(df))
        if strat.check_entry(df, i, PARAMS) is not None
    )
    assert sig.take_profit == 0.0
    assert sig.stop_loss == pytest.approx(
        sig.entry_price * (1 - PARAMS.tqqq_stop_loss_pct)
    )
    assert sig.strategy == "tqqq_momentum"


def test_no_entry_when_tsi_stays_below_signal():
    strat = get_strategy("tqqq_momentum")
    df = _frame([100.0 * (0.99 ** i) for i in range(WARMUP + 60)])
    assert all(
        strat.check_entry(df, i, PARAMS) is None
        for i in range(WARMUP, len(df))
    )


# ── exit ─────────────────────────────────────────────────────────────────────

def test_exit_fires_when_close_breaks_below_ema():
    strat = get_strategy("tqqq_momentum")
    df = _frame(_ramp_then(0.93))  # sharp drop after the uptrend
    reasons = [strat.check_exit(df, i, PARAMS) for i in range(WARMUP, len(df))]
    assert "ema_break" in reasons


def test_no_exit_while_above_ema():
    strat = get_strategy("tqqq_momentum")
    df = _frame([100.0 * (1.004 ** i) for i in range(WARMUP + 60)])
    assert all(
        strat.check_exit(df, i, PARAMS) is None
        for i in range(WARMUP, len(df))
    )


def test_exit_reason_matches_declared_label():
    strat = get_strategy("tqqq_momentum")
    df = _frame(_ramp_then(0.93))
    reasons = {strat.check_exit(df, i, PARAMS) for i in range(WARMUP, len(df))}
    assert strat.signal_exit_reason in reasons
    assert strat.signal_exit_reason == "ema_break"


# ── wiring ───────────────────────────────────────────────────────────────────

def test_strategy_metadata():
    strat = get_strategy("tqqq_momentum")
    assert strat.timeframe == "4h"
    assert strat.exit_mode == "signal_with_stop"
    assert strat.has_take_profit is False
    assert strat.stop_loss_fraction(PARAMS) == PARAMS.tqqq_stop_loss_pct


def test_scoped_to_leveraged_tickers_only():
    strat = get_strategy("tqqq_momentum")
    assert strat.universe() == list(LEVERAGED_TICKERS)
    assert "TQQQ" in strat.universe()
    # ...and it must not reach into the shared equity universe.
    assert not set(strat.universe()) & set(TICKERS)


def test_other_strategies_never_trade_leveraged_tickers():
    leveraged = set(LEVERAGED_TICKERS)
    for name, strat in REGISTRY.items():
        if name == "tqqq_momentum":
            continue
        assert not set(strat.universe()) & leveraged, (
            f"{name} would trade a leveraged ETF"
        )
        assert strat.universe() == list(TICKERS)


def test_all_tickers_covers_every_strategy_universe():
    covered = set()
    for strat in REGISTRY.values():
        covered |= set(strat.universe())
    assert covered <= set(ALL_TICKERS)


def test_default_stop_fraction_unchanged_for_sma_cross():
    """The new per-strategy stop hook must not move sma_50_cross."""
    strat = get_strategy("sma_50_cross")
    assert strat.stop_loss_fraction(PARAMS) == PARAMS.sma_cross_stop_loss_pct


def test_backtest_uses_strategy_stop_fraction():
    """The signal-exit backtest path honours the strategy's own stop."""
    from backtest_portfolio import collect_backtest_candidates

    strat = get_strategy("tqqq_momentum")
    df = _frame(_walk())
    cands = collect_backtest_candidates(
        df, "TQQQ", df.index[WARMUP], df.index[-1], PARAMS, strat
    )
    assert cands, "expected at least one candidate"
    c = cands[0]
    assert c.take_profit == 0.0
    assert c.stop_loss == pytest.approx(
        c.entry_price * (1 - PARAMS.tqqq_stop_loss_pct)
    )
