"""F16 strategy predicates: effective guards, parameters, and documented rules."""
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from config import PARAMS
from strategies.breakout import BreakoutStrategy
from strategies.ensemble import EnsembleStrategy
from strategies.mean_reversion import MeanReversionStrategy
from strategies.regime_adaptive import RegimeAdaptiveStrategy


def _frame(**overrides) -> pd.DataFrame:
    values = dict(
        open=99.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=200.0,
        sma_slow=90.0,
        sma_fast=110.0,
        sma_vol=100.0,
        atr=2.0,
        bb_lower=95.0,
        rsi=55.0,
        ema_short=95.0,
        ema_long=90.0,
    )
    values.update(overrides)
    return pd.DataFrame(values, index=pd.date_range("2026-01-01", periods=65))


def test_breakout_rejects_current_range_against_prior_completed_ranges():
    frame = _frame()
    frame.loc[frame.index[-2], "rsi"] = 54.0
    frame.loc[frame.index[-1], ["high", "low"]] = [200.0, 99.0]

    assert BreakoutStrategy().check_entry(frame, 64, PARAMS) is None


def test_breakout_range_multiplier_changes_the_range_guard_decision():
    frame = _frame()
    frame.loc[frame.index[-2], "rsi"] = 54.0
    frame.loc[frame.index[-1], ["high", "low"]] = [102.0, 99.0]

    strict = BreakoutStrategy().check_entry(
        frame, 64, replace(PARAMS, breakout_range_mult=1.3)
    )
    permissive = BreakoutStrategy().check_entry(
        frame, 64, replace(PARAMS, breakout_range_mult=2.0)
    )

    assert strict is None
    assert permissive is not None


@pytest.mark.parametrize(("row_offset", "field"), [(0, "low"), (-1, "high")])
def test_breakout_range_guard_rejects_missing_current_or_prior_evidence(
    row_offset, field
):
    frame = _frame()
    frame.loc[frame.index[-2], "rsi"] = 54.0
    frame.loc[frame.index[-1], ["high", "low"]] = [102.0, 100.0]
    frame.loc[frame.index[-1 + row_offset], field] = np.nan

    assert BreakoutStrategy().check_entry(frame, 64, PARAMS) is None


def test_breakout_remains_an_intrabar_high_break_rule():
    frame = _frame()
    frame.loc[frame.index[-2], "rsi"] = 54.0
    frame.loc[frame.index[-1], ["high", "low", "close"]] = [102.0, 100.0, 100.0]

    signal = BreakoutStrategy().check_entry(frame, 64, PARAMS)

    assert signal is not None
    assert signal.entry_price == 100.0


def test_mean_reversion_does_not_gate_on_diagnostic_bollinger_value():
    frame = _frame(bb_lower=np.nan, rsi=40.0)
    frame.loc[frame.index[-2], "close"] = 98.0

    assert MeanReversionStrategy().check_entry(frame, 64, PARAMS) is not None


class _Member:
    def __init__(self, signals: bool):
        self._signals = signals

    def check_entry(self, *_args):
        return object() if self._signals else None


def test_ensemble_requires_configured_vote_count_in_addition_to_weight():
    strategy = EnsembleStrategy()
    strategy._members = {"regime": _Member(True), "breakout": _Member(False)}
    frame = _frame()

    assert strategy.check_entry(
        frame, 64, replace(PARAMS, ensemble_min_votes=2)
    ) is None
    assert strategy.check_entry(
        frame, 64, replace(PARAMS, ensemble_min_votes=1)
    ) is not None


def test_ensemble_still_requires_the_weighted_score_with_two_votes():
    strategy = EnsembleStrategy()
    strategy._members = {
        "breakout": _Member(True),
        "mean_reversion": _Member(True),
    }

    assert strategy.check_entry(_frame(), 64, PARAMS) is None


def test_regime_risk_off_multiplier_controls_defensive_stop_distance():
    frame = _frame(rsi=35.0, sma_slow=98.0, ema_short=95.0, ema_long=100.0)
    frame.loc[frame.index[-2], "close"] = 89.0
    frame.loc[frame.index[-1], "close"] = 90.0
    params = replace(PARAMS, stop_loss_pct=0.10, regime_risk_off_mult=0.70)

    signal = RegimeAdaptiveStrategy().check_entry(frame, 64, params)

    assert signal is not None
    assert signal.stop_loss == pytest.approx(83.7)


@pytest.mark.parametrize(
    ("frame", "expected_stop"),
    [
        (_frame(), 88.0),
        (_frame(ema_short=95.0, ema_long=100.0), 90.0),
    ],
)
def test_regime_other_branches_retain_their_stop_distances(frame, expected_stop):
    frame.loc[frame.index[-2], "close"] = 99.0

    signal = RegimeAdaptiveStrategy().check_entry(frame, 64, PARAMS)

    assert signal is not None
    assert signal.stop_loss == pytest.approx(expected_stop)
