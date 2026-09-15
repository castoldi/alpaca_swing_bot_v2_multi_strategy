"""F08: valid t tails survive; untestable panels cannot become evidence."""
import math

import numpy as np
import pandas as pd
import pytest
from scipy.stats import ttest_1samp

from research.significance import backtest_verdict, bootstrap_max_t_hurdle, evaluate, evaluate_search, t_statistic


@pytest.mark.parametrize('sample', [
    [.01, .02, .03, .04], [-.01, -.02, -.03, -.04],
    [-.04, .01, -.02, .03], [1, 1.00001, 1.00002, 1.00003],
    [1e-100, 2e-100, 3e-100, 4e-100],
])
def test_finite_statistics_above_and_below_sqrt_n_match_scipy(sample):
    expected = ttest_1samp(sample, 0, alternative='greater')
    assert t_statistic(sample) == pytest.approx((expected.statistic, expected.pvalue))


@pytest.mark.parametrize('sample', [[0.] * 30, [.02] * 30, [-.02] * 30,
                                          [], [1.], [None, np.nan, np.inf]])
def test_untestable_observed_samples_cannot_claim_significance(sample):
    report = evaluate(sample)
    assert (report.t_stat, report.p_value) == (0., 1.)
    assert not report.significant
    assert report.sharpe == 0
    assert 'not testable' in report.summary()
    assert 'Best-of-N selection explains' not in report.summary()


def test_single_report_retains_t_and_adjusted_p_above_the_old_limit():
    report = evaluate([.01, .02, .03, .04], trials=2)
    assert report.t_stat == pytest.approx(3.8729833462)
    assert report.adjusted_p_value == pytest.approx(.0304662916)
    assert report.significant


def panel(first, second):
    return {'variant': [(pd.Timestamp('2025-01-15'), x) for x in first]
            + [(pd.Timestamp('2025-02-15'), x) for x in second]}


def draw_second_month(monkeypatch):
    class Draw:
        def integers(self, low, high, size):
            return np.ones(size, dtype=int)
    monkeypatch.setattr(np.random, 'default_rng', lambda seed: Draw())


def test_bootstrap_preserves_large_finite_tail_observations(monkeypatch):
    draw_second_month(monkeypatch)
    configs = panel([-.0102, -.0098] * 5, [.0098, .0102] * 5)
    hurdle, maxima = bootstrap_max_t_hurdle(configs, n_boot=10)
    expected = ttest_1samp([.0098, .0102] * 10, 0, alternative='greater').statistic
    assert expected > math.sqrt(20)
    assert maxima == pytest.approx([expected] * 10)
    assert hurdle == pytest.approx(expected)


def test_positive_constant_null_resample_cannot_lower_the_hurdle(monkeypatch):
    draw_second_month(monkeypatch)
    configs = panel([-.01] * 10, [.01] * 10)
    hurdle, maxima = bootstrap_max_t_hurdle(configs, n_boot=10)
    assert math.isinf(hurdle)
    assert np.isposinf(maxima).all()
    report, _ = evaluate_search(configs, n_boot=10)
    assert not report.significant


def test_underfilled_null_resample_cannot_lower_the_hurdle(monkeypatch):
    draw_second_month(monkeypatch)
    configs = panel([-.01, .01] * 10, [.02])
    hurdle, maxima = bootstrap_max_t_hurdle(configs, n_boot=10)
    assert math.isinf(hurdle)
    assert np.isposinf(maxima).all()


@pytest.mark.parametrize('configs', [
    panel([.01] * 10, [.01] * 10),
    panel([.01, .02] * 10, []),  # enough trades, only one original month
    panel([.01, .02], [.03, .04]),  # two months, too few trades
])
def test_original_panel_requires_variation_trades_and_multiple_months(configs):
    hurdle, maxima = bootstrap_max_t_hurdle(configs, n_boot=10)
    assert math.isinf(hurdle)
    assert maxima.size == 0
    report, _ = evaluate_search(configs, n_boot=10)
    assert not report.significant


def test_ineligible_winner_cannot_borrow_other_variants_hurdle():
    dates = pd.date_range('2025-01-01', periods=40, freq='3D')
    configs = {'eligible': list(zip(dates, [-.02, .03] * 20)),
               'thin_winner': [(dates[0], .1), (dates[-1], .10001)]}
    report, adjusted = evaluate_search(configs, winner='thin_winner', n_boot=30)
    assert report.trials == 2
    assert set(adjusted) == set(configs)
    assert not report.significant
    assert 'not testable' in report.summary()
    assert 'Best-of-N selection explains' not in report.summary()


def test_failed_calibration_summary_does_not_claim_selection_explains_it(monkeypatch):
    draw_second_month(monkeypatch)
    report, _ = evaluate_search(panel([-.01] * 10, [.01] * 10), n_boot=10)
    assert 'not calibrated' in report.summary()
    assert 'Best-of-N selection explains' not in report.summary()


def test_year_report_names_the_reason_a_winner_cannot_be_tested():
    from types import SimpleNamespace
    trades = [SimpleNamespace(entry_date=pd.Timestamp('2025-01-15'), pnl_pct=r)
              for r in [.01, .02] * 20]
    text = backtest_verdict({'one_month': trades}, n_boot=10)
    assert 'fewer than two calendar months' in text
    assert 'Best-of-N selection explains' not in text


def test_negative_constant_null_resample_contributes_no_positive_tail(monkeypatch):
    draw_second_month(monkeypatch)
    hurdle, maxima = bootstrap_max_t_hurdle(panel([.01] * 10, [-.01] * 10), n_boot=10)
    assert hurdle == 0
    assert (maxima == 0).all()


def test_near_zero_scale_does_not_underflow_into_no_evidence():
    assert t_statistic(np.array([1., 2., 3., 4.]) * 1e-300) == pytest.approx(
        (3.8729833462, .015233145831))


@pytest.mark.parametrize('scale', [1e-300, 1e308])
def test_single_report_statistics_remain_consistent_when_rescaled(scale):
    values = np.array([1., 1.1, 1.2, 1.3] * 10)
    baseline = evaluate(values)
    report = evaluate(values * scale)
    assert report.mean_return == pytest.approx(1.15 * scale)
    assert report.t_stat == pytest.approx(baseline.t_stat)
    assert report.sharpe == pytest.approx(baseline.sharpe)
    assert report.haircut_sharpe == pytest.approx(baseline.haircut_sharpe)


@pytest.mark.parametrize('scale', [1e-300, 1e308])
def test_rescaling_preserves_bootstrap_centering_and_search_report(scale):
    values = np.array([1., 1.1, 1.2, 1.3] * 10)
    dates = pd.date_range('2025-01-01', periods=len(values), freq='3D')
    original = {'variant': list(zip(dates, values))}
    scaled = {'variant': list(zip(dates, values * scale))}
    hurdle, maxima = bootstrap_max_t_hurdle(original, n_boot=100)
    scaled_hurdle, scaled_maxima = bootstrap_max_t_hurdle(scaled, n_boot=100)
    assert scaled_maxima == pytest.approx(maxima, abs=1e-10)
    assert scaled_hurdle == pytest.approx(hurdle)
    baseline, _ = evaluate_search(original, n_boot=100)
    report, _ = evaluate_search(scaled, n_boot=100)
    assert report.t_stat == pytest.approx(baseline.t_stat)
    assert math.isfinite(report.mean_return)
    assert report.sharpe == pytest.approx(baseline.sharpe)
    assert report.haircut_sharpe == pytest.approx(baseline.haircut_sharpe)
    assert report.significant == baseline.significant


def test_mixed_finite_and_infinite_tail_uses_a_defined_quantile(monkeypatch):
    class Draw:
        count = 0
        def integers(self, low, high, size):
            self.count += 1
            return np.array([0, 1] if self.count < 10 else [1, 1])
    monkeypatch.setattr(np.random, 'default_rng', lambda seed: Draw())
    hurdle, maxima = bootstrap_max_t_hurdle(panel([-.01] * 10, [.01] * 10), n_boot=10)
    assert np.isfinite(maxima[:9]).all()
    assert np.isposinf(maxima[-1])
    assert np.isposinf(hurdle)


@pytest.mark.parametrize('argument,value', [('alpha', 0), ('alpha', 1),
                                          ('min_trades', 1), ('min_trades', 2.5)])
def test_invalid_calibration_limits_are_rejected(argument, value):
    with pytest.raises(ValueError, match=argument):
        bootstrap_max_t_hurdle(panel([-.01, .02] * 10, [-.02, .01] * 10),
                               n_boot=10, **{argument: value})


@pytest.mark.parametrize('n_boot', [0, -1, 1.5, True])
def test_invalid_bootstrap_count_is_rejected_explicitly(n_boot):
    with pytest.raises(ValueError, match='n_boot'):
        bootstrap_max_t_hurdle(panel([-.01, .02] * 10, [-.02, .01] * 10), n_boot=n_boot)
