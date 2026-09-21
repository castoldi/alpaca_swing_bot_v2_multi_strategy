"""RSI edge values, unchanged smoothing, and unavailable-signal safety."""
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from config import PARAMS
from strategies import REGISTRY
from strategies.base import add_indicators, rsi


@pytest.mark.parametrize('period', [1, 3, 14])
@pytest.mark.parametrize(('step', 'expected'), [(1, 100.), (-1, 0.), (0, 50.)])
def test_one_direction_or_flat_history_has_explicit_warmup(period, step, expected):
    close = pd.Series(100. + step * np.arange(80),
                      index=pd.date_range('2026-01-01', periods=80), name='close')
    original = close.copy()
    result = rsi(close, period)
    assert result.iloc[:period].isna().all()
    assert (result.iloc[period:] == expected).all()
    pd.testing.assert_index_equal(result.index, close.index)
    pd.testing.assert_series_equal(close, original)


@pytest.mark.parametrize('values', [[], [100.], [100.] * 14, [np.nan] * 30])
def test_insufficient_history_stays_unavailable(values):
    assert rsi(pd.Series(values, dtype=float)).isna().all()


def test_mixed_prices_match_hand_calculated_existing_ewm_seed():
    # period=3, seed first change (+2), then recurse with weights 2/3 and 1/3.
    # At index 3: gain=17/9, loss=2/9 -> RSI=1700/19.
    # At index 4: gain=34/27, loss=22/27 -> RSI=3400/56.
    # At index 5: gain=149/81, loss=44/81 -> RSI=14900/193.
    result = rsi(pd.Series([100., 102., 101., 104., 102., 105.]), period=3)
    assert result.iloc[:3].isna().all()
    assert result.iloc[3:].tolist() == pytest.approx([1700/19, 3400/56, 14900/193])


@pytest.mark.parametrize('period', [3, 14, 28])
def test_nonzero_gain_and_loss_match_previous_formula(period):
    rng = np.random.default_rng(14)
    close = pd.Series(100 + np.r_[0., 1., -1., rng.normal(size=200)].cumsum())
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    legacy = 100 - 100 / (1 + gain / loss)
    result = rsi(close, period)
    pd.testing.assert_series_equal(result, legacy)
    assert result.dropna().between(0, 100).all()


@pytest.mark.parametrize(('move', 'expected'), [(1, 100.), (-1, 0.)])
def test_first_move_after_flat_prices_has_direction(move, expected):
    result = rsi(pd.Series([100.] * 30 + [100. + move] * 5))
    assert result.iloc[29] == 50
    assert (result.iloc[30:] == expected).all()


def test_leading_missing_prices_do_not_count_toward_warmup():
    result = rsi(pd.Series([np.nan] * 3 + [100., 101., 102., 103., 104.]), period=3)
    assert result.iloc[:6].isna().all()
    assert result.iloc[6:].tolist() == [100., 100.]


def entry_frame(name):
    """Controlled, otherwise qualifying indicators at the strategy boundary."""
    frame = pd.DataFrame(dict(
        open=98., high=99., low=97., close=98., volume=200.,
        sma_slow=90., sma_fast=95., sma_vol=100., atr=2., bb_lower=100.,
        rsi=40., macd=1., macd_hist=-1., ema_short=95., ema_long=90., near_earnings=False,
    ), index=pd.date_range('2026-01-01', periods=65))
    frame.loc[frame.index[-1], ['open', 'high', 'low', 'close', 'rsi', 'macd_hist']] = [99, 101, 99, 100, 60, 1]
    if name == 'mean_reversion':
        frame['sma_fast'] = 110.
    return frame


@pytest.mark.parametrize(('name', 'missing'), [
    ('breakout', 'current'), ('breakout', 'previous'),
    ('mean_reversion', 'current'), ('mean_reversion', 'window'),
    ('trend_pullback', 'current'), ('momentum_macd', 'current'),
    ('regime', 'current'), ('ensemble', 'current'),
])
def test_missing_required_rsi_cannot_authorize_an_entry(name, missing):
    frame = entry_frame(name)
    strategy = REGISTRY[name]
    assert strategy.check_entry(frame, 64, PARAMS) is not None
    if missing == 'window':
        frame['rsi'] = np.nan
    else:
        frame.loc[frame.index[-2 if missing == 'previous' else -1], 'rsi'] = np.nan
    assert strategy.check_entry(frame, 64, PARAMS) is None


def test_breakout_preserves_finite_threshold_and_rising_requirement():
    frame = entry_frame('breakout')
    frame.loc[frame.index[-2:], 'rsi'] = [49., 50.]
    assert REGISTRY['breakout'].check_entry(frame, 64, PARAMS) is not None
    frame.loc[frame.index[-2:], 'rsi'] = [100., 100.]
    assert REGISTRY['breakout'].check_entry(frame, 64, PARAMS) is None


def test_mean_reversion_preserves_inclusive_oversold_threshold():
    frame = entry_frame('mean_reversion')
    params = replace(PARAMS, mr_rsi_oversold=50.)
    frame['rsi'] = 50.
    assert REGISTRY['mean_reversion'].check_entry(frame, 64, params) is not None
    frame['rsi'] = 50.01
    assert REGISTRY['mean_reversion'].check_entry(frame, 64, params) is None


@pytest.mark.parametrize('name', ['trend_pullback', 'breakout', 'mean_reversion',
                                 'momentum_macd', 'regime', 'ensemble'])
def test_extended_rsi_warmup_cannot_be_bypassed_by_other_ready_indicators(name):
    raw = entry_frame(name)[['open', 'high', 'low', 'close', 'volume']]
    params = replace(PARAMS, rsi_period=80)
    frame = add_indicators(raw, params)
    assert frame['rsi'].isna().all()
    assert REGISTRY[name].check_entry(frame, 64, params) is None
