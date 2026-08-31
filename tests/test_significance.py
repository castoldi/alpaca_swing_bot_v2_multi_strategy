"""Tests for the multiple-testing discipline (Harvey & Liu 2020).

The load-bearing test here is `test_noise_sweep_winner_is_rejected`: a sweep over
pure noise always produces a "best" configuration with an impressive-looking raw
t-statistic, and the whole point of this module is that it must not clear the
hurdle.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.significance import (
    analytic_max_t_hurdle,
    backtest_verdict,
    bhy,
    bonferroni,
    bootstrap_max_t_hurdle,
    evaluate,
    evaluate_search,
    holm,
    returns_from_trades,
    sharpe_ratio,
    t_statistic,
)


# ── t-statistic ───────────────────────────────────────────────────────────────

def test_t_statistic_matches_scipy():
    from scipy import stats as sps

    rng = np.random.default_rng(0)
    sample = rng.normal(0.01, 0.05, size=200)
    t, p = t_statistic(sample)
    expected_t, expected_p_two = sps.ttest_1samp(sample, 0.0)

    assert t == pytest.approx(float(expected_t), rel=1e-9)
    assert p == pytest.approx(float(expected_p_two) / 2, rel=1e-9)


@pytest.mark.parametrize("sample", [[], [0.05], [0.02, 0.02, 0.02]])
def test_t_statistic_degenerate_samples_are_not_evidence(sample):
    """Too few trades, or zero variance, must read as "no evidence", not inf."""
    t, p = t_statistic(sample)
    assert t == 0.0
    assert p == 1.0


def test_sharpe_ratio_is_mean_over_stdev():
    sample = [0.10, -0.05, 0.20, 0.00, 0.05]
    expected = float(np.mean(sample)) / float(np.std(sample, ddof=1))
    assert sharpe_ratio(sample) == pytest.approx(expected)


# ── Adjustments ───────────────────────────────────────────────────────────────

def test_bonferroni_scales_by_test_count_and_clamps():
    assert bonferroni([0.01, 0.02, 0.9]) == pytest.approx([0.03, 0.06, 1.0])


def test_holm_is_never_more_conservative_than_bonferroni():
    p_values = [0.001, 0.008, 0.02, 0.3, 0.7]
    for h, b in zip(holm(p_values), bonferroni(p_values)):
        assert h <= b + 1e-12


def test_adjustments_preserve_ordering():
    """A variant with a smaller raw p-value must never end up ranked worse."""
    p_values = [0.04, 0.001, 0.2, 0.009, 0.5]
    for adjust in (bonferroni, holm, bhy):
        adjusted = adjust(p_values)
        raw_order = sorted(range(len(p_values)), key=lambda i: p_values[i])
        adj_sorted = [adjusted[i] for i in raw_order]
        assert adj_sorted == sorted(adj_sorted)


def test_bhy_pays_the_dependence_penalty():
    """BHY's c(M) factor makes it stricter than plain BH on the largest p-value."""
    p_values = [0.01, 0.02, 0.03, 0.04]
    m = len(p_values)
    c_m = sum(1.0 / i for i in range(1, m + 1))
    assert bhy(p_values)[-1] == pytest.approx(min(1.0, c_m * p_values[-1]))


def test_empty_adjustments():
    assert bonferroni([]) == holm([]) == bhy([]) == []


# ── Hurdles ───────────────────────────────────────────────────────────────────

def test_hurdle_rises_with_trials():
    """More configurations tried => a higher bar to clear. The core discipline."""
    hurdles = [analytic_max_t_hurdle(n, df=100) for n in (1, 5, 30, 200)]
    assert hurdles == sorted(hurdles)
    assert hurdles[0] < 2.0 < hurdles[2]


def test_single_trial_hurdle_is_the_ordinary_t_test():
    from scipy import stats as sps

    assert analytic_max_t_hurdle(1, df=50, alpha=0.05) == pytest.approx(
        float(sps.t.isf(0.05, df=50))
    )


def _noise_configs(n_configs: int, n_trades: int, seed: int, edge: float = 0.0):
    """A sweep of `n_configs` variants over overlapping months of trade returns."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2025-01-01", periods=n_trades, freq="3D")
    return {
        f"cfg{i:02d}": list(zip(dates, rng.normal(edge, 0.05, size=n_trades)))
        for i in range(n_configs)
    }


def test_bootstrap_hurdle_exceeds_single_test_bar():
    configs = _noise_configs(n_configs=20, n_trades=120, seed=7)
    hurdle, maxima = bootstrap_max_t_hurdle(configs, n_boot=300, seed=1)

    assert maxima.size == 300
    assert hurdle > 1.64  # the naive one-sided 5% bar for a single test


def test_bootstrap_hurdle_is_deterministic_for_a_seed():
    configs = _noise_configs(n_configs=8, n_trades=60, seed=3)
    a, _ = bootstrap_max_t_hurdle(configs, n_boot=200, seed=42)
    b, _ = bootstrap_max_t_hurdle(configs, n_boot=200, seed=42)
    assert a == pytest.approx(b)


def test_degenerate_resample_cannot_explode_the_t_statistic():
    """A near-constant sample must read as no evidence, not t = 1e15.

    Regression: a 3-trade strategy's bootstrap resample landed on two distinct
    values with lopsided counts. Variance was tiny but non-zero, so an
    `sd == 0` check missed it, and the resulting t of order 1e15 set the
    best-of-N hurdle for the whole year to 8.44 instead of ~2.5.
    """
    almost_constant = [0.02] * 40 + [0.020000000001]
    t, p = t_statistic(almost_constant)
    assert t == 0.0 and p == 1.0

    # The one-sample t-statistic is bounded by sqrt(n); nothing may exceed it.
    rng = np.random.default_rng(17)
    for _ in range(50):
        sample = rng.normal(0.01, 0.05, size=rng.integers(3, 60))
        t, _ = t_statistic(sample)
        assert abs(t) <= np.sqrt(len(sample))


def test_thin_strategies_do_not_set_the_hurdle():
    """A 3-trade strategy alongside real ones must not inflate the hurdle."""
    rng = np.random.default_rng(23)
    dates = pd.date_range("2026-01-01", periods=110, freq="2D")
    healthy = {
        f"real{i}": list(zip(dates, rng.normal(0.0, 0.05, size=110)))
        for i in range(3)
    }
    hurdle_clean, _ = bootstrap_max_t_hurdle(healthy, n_boot=400, seed=9)

    thin_dates = [pd.Timestamp("2026-01-15"), pd.Timestamp("2026-02-10"),
                  pd.Timestamp("2026-04-02")]
    with_thin = dict(healthy)
    with_thin["barely_traded"] = list(zip(thin_dates, [-0.02, 0.04, -0.01]))
    hurdle_thin, maxima = bootstrap_max_t_hurdle(with_thin, n_boot=400, seed=9)

    assert hurdle_thin == pytest.approx(hurdle_clean)
    assert np.isfinite(maxima).all()
    assert maxima.max() < 20.0        # was ~1e15 before the guard


def test_bootstrap_handles_empty_and_tiny_configs():
    hurdle, maxima = bootstrap_max_t_hurdle({}, n_boot=10)
    assert hurdle == float("inf")
    assert maxima.size == 0

    only_one_trade = {"cfg0": [(pd.Timestamp("2025-01-01"), 0.05)]}
    hurdle, _ = bootstrap_max_t_hurdle(only_one_trade, n_boot=10)
    assert hurdle == float("inf")


# ── End-to-end ────────────────────────────────────────────────────────────────

def test_noise_sweep_winner_is_rejected():
    """30 variants of pure noise: the winner looks good and must still fail.

    This is the failure mode the module exists to catch — `random_search` sorts
    by P&L and hands back the top config, which under the null is a draw from
    the maximum of 30 correlated t-statistics, not from a single one.
    """
    configs = _noise_configs(n_configs=30, n_trades=150, seed=11)
    report, adjusted = evaluate_search(configs, n_boot=400, seed=5)

    assert report.trials == 30
    assert len(adjusted) == 30
    assert report.t_stat > 0            # the winner does look positive
    assert not report.significant       # ...and is correctly rejected
    assert report.haircut_sharpe < report.sharpe
    assert "-> Best-of-N selection explains this result" in report.summary()


def test_genuine_edge_survives_the_same_sweep():
    """A real, large edge must still clear the hurdle, or the module is useless.

    Harvey & Liu's second contribution is Type II error: a correction that
    rejects everything is not a good correction.
    """
    configs = _noise_configs(n_configs=30, n_trades=150, seed=11, edge=0.0)
    rng = np.random.default_rng(99)
    dates = pd.date_range("2025-01-01", periods=150, freq="3D")
    configs["winner"] = list(zip(dates, rng.normal(0.04, 0.05, size=150)))

    report, _ = evaluate_search(configs, winner="winner", n_boot=400, seed=5)

    assert report.significant
    assert report.haircut_sharpe > 0
    assert "CLEARS" in report.summary()


def _sample_with_t(target_t: float, n: int = 200, sd: float = 0.05, seed: int = 4):
    """A return series whose t-statistic is exactly `target_t`.

    Standardise a draw, then rescale — so the test states the significance level
    it is probing instead of hoping a chosen mean lands there.
    """
    rng = np.random.default_rng(seed)
    raw = rng.normal(0.0, 1.0, size=n)
    z = (raw - raw.mean()) / raw.std(ddof=1)
    return z * sd + target_t * sd / np.sqrt(n)


def test_evaluate_penalises_an_undisclosed_search():
    """Identical returns, different trials count => different verdict.

    t = 2.5 is comfortably "significant" by the conventional single-test bar and
    is exactly the regime Harvey & Liu target: it survives one honest test and
    should not survive being the pick of 100.
    """
    returns = _sample_with_t(2.5)

    honest_single = evaluate(returns, trials=1)
    after_search = evaluate(returns, trials=100)

    assert honest_single.t_stat == pytest.approx(2.5)

    assert honest_single.t_stat == pytest.approx(after_search.t_stat)
    assert after_search.hurdle_t > honest_single.hurdle_t
    assert after_search.adjusted_p_value > honest_single.adjusted_p_value
    assert after_search.haircut_sharpe < honest_single.haircut_sharpe
    assert honest_single.significant and not after_search.significant


def test_evaluate_reports_annualised_sharpe_when_span_given():
    rng = np.random.default_rng(8)
    returns = rng.normal(0.02, 0.05, size=100)

    assert evaluate(returns).sharpe_annual is None
    report = evaluate(returns, span_years=2.0)
    assert report.sharpe_annual == pytest.approx(report.t_stat / np.sqrt(2.0))


def test_evaluate_empty_backtest_is_not_significant():
    report = evaluate([], trials=10)
    assert report.n_trades == 0
    assert not report.significant
    assert report.haircut_sharpe == 0.0


# ── Backtest reporting block ──────────────────────────────────────────────────

def _fake_trades(returns, start="2025-01-01"):
    """Minimal stand-ins carrying just what `returns_from_trades` reads."""
    class _T:
        def __init__(self, when, pnl):
            self.entry_date = when
            self.pnl_pct = pnl

    dates = pd.date_range(start, periods=len(returns), freq="3D")
    return [_T(d, r) for d, r in zip(dates, returns)]


def test_backtest_verdict_renders_every_strategy_and_the_hurdle():
    rng = np.random.default_rng(21)
    by_strategy = {
        name: _fake_trades(rng.normal(0.0, 0.05, size=80))
        for name in ("ensemble", "breakout", "regime", "mean_reversion")
    }
    text = backtest_verdict(by_strategy, winner="ensemble", n_boot=200)

    for name in by_strategy:
        assert name in text
    assert "Best-of-4 hurdle" in text
    assert "<-- best P&L" in text
    assert "FLOOR on the trials count" in text  # the honesty note must survive


def test_backtest_verdict_marks_the_pnl_winner_not_the_top_t():
    """The block reports the strategy the script picked, even if another has a
    higher t-statistic — that mismatch is exactly what a reader should see."""
    rng = np.random.default_rng(5)
    by_strategy = {
        "picked_on_pnl": _fake_trades(rng.normal(0.001, 0.05, size=80)),
        "higher_t": _fake_trades(rng.normal(0.03, 0.05, size=80)),
    }
    text = backtest_verdict(by_strategy, winner="picked_on_pnl", n_boot=200)

    winner_row = [ln for ln in text.splitlines() if "<-- best P&L" in ln]
    assert len(winner_row) == 1
    assert "picked_on_pnl" in winner_row[0]


def test_backtest_verdict_survives_an_empty_year():
    assert "nothing testable" in backtest_verdict({}, n_boot=50)
    assert "nothing testable" in backtest_verdict(
        {"ensemble": [], "breakout": _fake_trades([0.02])}, n_boot=50
    )


def test_backtest_verdict_names_the_strategies_it_could_not_test():
    """Thin strategies are excluded from the panel but reported, not hidden.

    Dropping them silently would understate the trials count and leave the
    reader wondering where a strategy went.
    """
    rng = np.random.default_rng(31)
    text = backtest_verdict(
        {
            "ensemble": _fake_trades(rng.normal(0.0, 0.05, size=60)),
            "breakout": _fake_trades(rng.normal(0.0, 0.05, size=60)),
            "barely_traded": _fake_trades([0.01]),   # 1 trade — no t-statistic
        },
        winner="ensemble",
        n_boot=200,
    )
    assert "Best-of-2 hurdle" in text          # thin one excluded from the max
    assert "Not tested, under 20 trades" in text
    assert "barely_traded (1)" in text          # ...but still named


def test_output_is_ascii_encodable():
    """Every logged string must survive the narrowest Windows console codepage.

    logger_setup mirrors to sys.stdout. A box-drawing or arrow character makes
    logging swallow the record and print "--- Logging error ---" instead, losing
    the number the block exists to show. Strict ASCII is the bar, not cp1252:
    cp1252 encodes an em-dash happily, but the console renders it as a replacement
    character, which is how one slipped through the first version of this test.
    """
    rng = np.random.default_rng(13)
    by_strategy = {
        "ensemble": _fake_trades(rng.normal(0.0, 0.05, size=70)),
        "breakout": _fake_trades(rng.normal(0.0, 0.05, size=70)),
    }
    block = backtest_verdict(by_strategy, winner="ensemble", n_boot=100)
    report, _ = evaluate_search(
        {"a": [(pd.Timestamp("2025-01-01"), 0.01)] * 30}, n_boot=50
    )

    for text in (block, report.summary(), evaluate([0.01, 0.02, 0.03]).summary()):
        text.encode("ascii")   # raises UnicodeEncodeError if a glyph slips in


def test_returns_from_trades_reads_trade_objects():
    from strategy import Trade

    trade = Trade(
        ticker="NVDA",
        entry_date=pd.Timestamp("2025-03-01"),
        entry_price=100.0,
        stop_loss=90.0,
        take_profit=110.0,
        exit_date=pd.Timestamp("2025-03-05"),
        exit_price=105.0,
        exit_reason="tp",
        bars_held=4,
        shares=2,
        pnl_dollars=10.0,
        pnl_pct=0.05,
    )
    assert returns_from_trades([trade]) == [(pd.Timestamp("2025-03-01"), 0.05)]
