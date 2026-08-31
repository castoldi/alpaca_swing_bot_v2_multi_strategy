"""Multiple-testing discipline for the research loop.

Implements the practical core of Harvey & Liu (2020), "False (and Missed)
Discoveries in Financial Economics" (arXiv:2006.04269), plus the three
multiple-testing adjustments from Harvey, Liu & Zhu (2016) that the 2020 paper
benchmarks.

Why this module exists
----------------------
The research loop in CLAUDE.md says "keep if both years improve". That is a
two-observation confirmation with no correction for how many variants were
tried to get there. `research/optimizer.py` sweeps a parameter grid and returns
the configurations sorted by P&L — reporting the top one is a *maximum* over N
correlated tests, not a single test. Under that selection rule the classic
t > 2.0 bar is badly wrong: Harvey & Liu find only 5.5% of 18,113 anomalies
clear t > 2.0, barely above the ~5% you would get from pure noise, and the
correctly calibrated hurdle for that dataset was t = 4.9.

The two entry points mirror the two situations we actually face:

`evaluate(returns, trials=N)`
    One candidate change, N variants tried to find it. No panel of per-variant
    returns is available (e.g. the change was found by hand, or by reading a
    paper). Uses a Bonferroni adjustment over N — conservative, because it
    ignores the covariance structure across variants, which the paper flags as
    Bonferroni's central weakness.

`evaluate_search(configs)`
    A full parameter sweep where every variant's trade returns are in hand.
    Runs BHY (FDR under arbitrary dependence) across the panel, and calibrates
    the selection hurdle by bootstrap: demean each variant's returns so the null
    p0 = 0 holds in sample, resample *calendar months jointly across variants*
    to preserve their cross-correlation, and read the hurdle off the resulting
    distribution of the maximum t-statistic. This is the paper's first bootstrap
    stage (their Y0 construction, following Fama-French (2010) and KTWW (2006)),
    applied to our own search rather than to a fund panel.

Known limitation, stated rather than hidden
-------------------------------------------
Trades overlap in time and run across correlated tickers (up to 5 concurrent
positions), so per-trade returns are not iid and the raw t-statistic is
inflated. The month-block bootstrap in `evaluate_search` absorbs part of this;
the single-report `evaluate` path does not, so treat its t-statistic as an
optimistic upper bound. Neither path turns a backtest into an out-of-sample
result — they only price the search that produced it.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd
from scipy import stats

# A dated return observation: (entry date, fractional return e.g. 0.031 for +3.1%)
DatedReturn = tuple[Any, float]


# ── Report ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SignificanceReport:
    """The audit record for one kept-or-rejected research decision."""

    n_trades: int
    trials: int
    mean_return: float
    sharpe: float               # per-trade Sharpe: mean / stdev of trade returns
    sharpe_annual: float | None  # only when the caller supplies the span in years
    t_stat: float
    p_value: float              # single-test, one-sided
    adjusted_p_value: float     # after correcting for `trials`
    hurdle_t: float             # t-statistic needed to clear `alpha` given `trials`
    haircut_sharpe: float       # Sharpe that survives the multiplicity correction
    alpha: float
    method: str
    significant: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        verdict = "CLEARS" if self.significant else "FAILS"
        lines = [
            f"{verdict} the {self.alpha:.0%} hurdle after {self.method}.",
            f"  trades={self.n_trades}  trials={self.trials}  "
            f"mean={self.mean_return:+.3%}  Sharpe={self.sharpe:.2f}",
            f"  t={self.t_stat:.2f} (p={self.p_value:.4f})  vs hurdle t={self.hurdle_t:.2f}",
            f"  adjusted p={self.adjusted_p_value:.4f}  "
            f"haircut Sharpe={self.haircut_sharpe:.2f} (from {self.sharpe:.2f})",
        ]
        if not self.significant:
            lines.append(
                "  → Best-of-N selection explains this result. Do not keep the change "
                "on this evidence alone."
            )
        return "\n".join(lines)


# ── Single-test statistics ────────────────────────────────────────────────────

def t_statistic(returns: Sequence[float]) -> tuple[float, float]:
    """One-sided t-statistic and p-value for H0: mean trade return <= 0.

    Returns (0.0, 1.0) for samples too small or too degenerate to test, so
    callers never have to special-case an empty backtest.
    """
    arr = np.asarray([r for r in returns if r is not None], dtype=float)
    arr = arr[np.isfinite(arr)]
    n = arr.size
    if n < 2:
        return 0.0, 1.0

    sd = float(np.std(arr, ddof=1))
    if sd <= 0:
        # Every trade returned exactly the same amount. Degenerate, not evidence.
        return 0.0, 1.0

    t = float(np.mean(arr)) / (sd / math.sqrt(n))
    p = float(stats.t.sf(t, df=n - 1))
    return t, p


def sharpe_ratio(returns: Sequence[float]) -> float:
    """Per-trade Sharpe: mean / stdev of trade returns (no annualisation)."""
    arr = np.asarray([r for r in returns if r is not None], dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size < 2:
        return 0.0
    sd = float(np.std(arr, ddof=1))
    return float(np.mean(arr)) / sd if sd > 0 else 0.0


# ── Multiple-testing adjustments (Harvey, Liu & Zhu 2016) ─────────────────────

def bonferroni(p_values: Sequence[float]) -> list[float]:
    """Control FWER by scaling every p-value by the number of tests.

    Simplest and most conservative. Ignores the covariance structure across
    tests, which is exactly the weakness Harvey & Liu single out — grid-search
    variants share most of their trades, so this over-penalises them.
    """
    m = len(p_values)
    if m == 0:
        return []
    return [min(1.0, m * p) for p in p_values]


def holm(p_values: Sequence[float]) -> list[float]:
    """Step-down FWER control. Uniformly more powerful than Bonferroni."""
    m = len(p_values)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted = [0.0] * m
    running = 0.0
    for rank, idx in enumerate(order):
        candidate = (m - rank) * p_values[idx]
        running = max(running, candidate)      # enforce monotonicity
        adjusted[idx] = min(1.0, running)
    return adjusted


def bhy(p_values: Sequence[float]) -> list[float]:
    """Benjamini-Hochberg-Yekutieli: FDR control under *arbitrary* dependence.

    The right default for a parameter sweep, where variants are heavily
    correlated. The c(M) = sum(1/i) factor is the price of not assuming
    independence.
    """
    m = len(p_values)
    if m == 0:
        return []
    c_m = sum(1.0 / i for i in range(1, m + 1))
    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted = [0.0] * m
    running = 1.0
    for rank in range(m - 1, -1, -1):          # step up, largest p first
        idx = order[rank]
        candidate = (m * c_m / (rank + 1)) * p_values[idx]
        running = min(running, candidate)      # enforce monotonicity
        adjusted[idx] = min(1.0, running)
    return adjusted


ADJUSTMENTS = {"bonferroni": bonferroni, "holm": holm, "bhy": bhy}


# ── Selection hurdles ─────────────────────────────────────────────────────────

def analytic_max_t_hurdle(trials: int, df: int, alpha: float = 0.05) -> float:
    """t needed for the best of `trials` *independent* tests to clear `alpha`.

    P(max t < c) = F(c)^N under independence, so c = F^-1(alpha_adj) with
    alpha_adj = (1 - alpha)^(1/N). Independence is the worst case for a grid
    search — real variants overlap heavily — so this is an upper bound on how
    strict the hurdle should be. Prefer `bootstrap_max_t_hurdle` when the panel
    of per-variant returns is available.
    """
    if trials < 1 or df < 1:
        return float("inf")
    if trials == 1:
        return float(stats.t.isf(alpha, df=df))
    return float(stats.t.ppf((1.0 - alpha) ** (1.0 / trials), df=df))


def _period_key(when: Any) -> str:
    """Bucket a trade's entry into a calendar month, the bootstrap block unit."""
    if isinstance(when, (pd.Timestamp, datetime, date)):
        return f"{when.year:04d}-{when.month:02d}"
    ts = pd.Timestamp(when)
    return f"{ts.year:04d}-{ts.month:02d}"


def bootstrap_max_t_hurdle(
    configs: Mapping[str, Sequence[DatedReturn]],
    alpha: float = 0.05,
    n_boot: int = 1000,
    seed: int = 42,
) -> tuple[float, np.ndarray]:
    """Calibrate the best-of-N hurdle by bootstrap, preserving cross-correlation.

    Follows Harvey & Liu's first bootstrap stage. Each variant's returns are
    demeaned so the null (no edge, p0 = 0) holds exactly in sample; calendar
    months are then resampled *jointly across all variants*, so variants that
    share trades stay correlated in the resample the way they are in the data.
    The hurdle is the (1 - alpha) quantile of the resulting maximum t-statistic.

    Returns (hurdle, distribution of bootstrapped maxima).
    """
    if not configs:
        return float("inf"), np.array([])

    # Demean per variant (impose the null), then index returns by month.
    by_period: dict[str, dict[str, list[float]]] = {}
    months: set[str] = set()
    for name, observations in configs.items():
        values = [
            (when, float(r))
            for when, r in observations
            if r is not None and np.isfinite(float(r))
        ]
        if len(values) < 2:
            by_period[name] = {}
            continue
        mean = float(np.mean([r for _, r in values]))
        buckets: dict[str, list[float]] = defaultdict(list)
        for when, r in values:
            key = _period_key(when)
            buckets[key].append(r - mean)
            months.add(key)
        by_period[name] = {k: v for k, v in buckets.items()}

    month_list = sorted(months)
    if not month_list:
        return float("inf"), np.array([])

    rng = np.random.default_rng(seed)
    n_months = len(month_list)
    maxima = np.empty(n_boot, dtype=float)

    for b in range(n_boot):
        drawn = [month_list[i] for i in rng.integers(0, n_months, size=n_months)]
        best = 0.0
        for name in configs:
            buckets = by_period.get(name) or {}
            if not buckets:
                continue
            sample: list[float] = []
            for month in drawn:
                block = buckets.get(month)
                if block:
                    sample.extend(block)
            if len(sample) < 2:
                continue
            t, _ = t_statistic(sample)
            if t > best:
                best = t
        maxima[b] = best

    hurdle = float(np.quantile(maxima, 1.0 - alpha))
    return hurdle, maxima


# ── Public entry points ───────────────────────────────────────────────────────

def _haircut(sharpe: float, t_stat: float, adjusted_p: float, df: int) -> float:
    """HLZ haircut: scale Sharpe by t_adjusted / t_observed.

    t_adjusted is the t-statistic that the multiplicity-corrected p-value
    corresponds to under a single test. A change that only looked good because
    of the search collapses toward zero here.
    """
    if sharpe <= 0 or t_stat <= 0 or df < 1:
        return 0.0
    adjusted_p = min(max(adjusted_p, 1e-12), 1.0 - 1e-12)
    t_adj = float(stats.t.isf(adjusted_p, df=df))
    if t_adj <= 0:
        return 0.0
    return float(sharpe * min(1.0, t_adj / t_stat))


def evaluate(
    returns: Sequence[float],
    trials: int = 1,
    alpha: float = 0.05,
    span_years: float | None = None,
) -> SignificanceReport:
    """Price a single candidate change against the number of variants tried.

    `trials` is the honest count of configurations, parameter values, strategy
    variants and years-of-data slices you looked at before settling on this one.
    Undercounting it is the single easiest way to fool yourself; when in doubt,
    round up.
    """
    arr = [float(r) for r in returns if r is not None and np.isfinite(float(r))]
    n = len(arr)
    trials = max(1, int(trials))

    t_stat, p_value = t_statistic(arr)
    sharpe = sharpe_ratio(arr)
    df = max(n - 1, 1)

    adjusted_p = bonferroni([p_value] * trials)[0] if trials > 1 else p_value
    hurdle = analytic_max_t_hurdle(trials, df, alpha)
    haircut = _haircut(sharpe, t_stat, adjusted_p, df)
    sharpe_annual = (t_stat / math.sqrt(span_years)) if span_years and span_years > 0 else None

    return SignificanceReport(
        n_trades=n,
        trials=trials,
        mean_return=float(np.mean(arr)) if arr else 0.0,
        sharpe=sharpe,
        sharpe_annual=sharpe_annual,
        t_stat=t_stat,
        p_value=p_value,
        adjusted_p_value=adjusted_p,
        hurdle_t=hurdle,
        haircut_sharpe=haircut,
        alpha=alpha,
        method=f"Bonferroni over {trials} trial(s)",
        significant=bool(n >= 2 and t_stat >= hurdle and adjusted_p <= alpha),
    )


def evaluate_search(
    configs: Mapping[str, Sequence[DatedReturn]],
    winner: str | None = None,
    alpha: float = 0.05,
    n_boot: int = 1000,
    seed: int = 42,
) -> tuple[SignificanceReport, dict[str, float]]:
    """Price the *winner* of a parameter sweep against the sweep that found it.

    `configs` maps a variant label to its (entry_date, return) observations —
    every variant tried, not just the good ones. Omitting the losers inflates
    the result exactly as badly as not correcting at all.

    Returns the winner's report plus the BHY-adjusted p-value for every variant.
    """
    if not configs:
        return evaluate([], trials=1, alpha=alpha), {}

    names = list(configs)
    raw_returns = {
        name: [float(r) for _, r in obs if r is not None and np.isfinite(float(r))]
        for name, obs in configs.items()
    }
    stats_by_name = {name: t_statistic(rs) for name, rs in raw_returns.items()}

    p_values = [stats_by_name[name][1] for name in names]
    bhy_adjusted = dict(zip(names, bhy(p_values)))

    if winner is None:
        winner = max(names, key=lambda n: stats_by_name[n][0])

    hurdle, _ = bootstrap_max_t_hurdle(configs, alpha=alpha, n_boot=n_boot, seed=seed)

    win_returns = raw_returns[winner]
    n = len(win_returns)
    df = max(n - 1, 1)
    t_stat, p_value = stats_by_name[winner]
    sharpe = sharpe_ratio(win_returns)
    adjusted_p = bhy_adjusted[winner]

    report = SignificanceReport(
        n_trades=n,
        trials=len(names),
        mean_return=float(np.mean(win_returns)) if win_returns else 0.0,
        sharpe=sharpe,
        sharpe_annual=None,
        t_stat=t_stat,
        p_value=p_value,
        adjusted_p_value=adjusted_p,
        hurdle_t=hurdle,
        haircut_sharpe=_haircut(sharpe, t_stat, adjusted_p, df),
        alpha=alpha,
        method=f"month-block bootstrap max-t over {len(names)} variants (BHY p-values)",
        significant=bool(n >= 2 and t_stat >= hurdle),
    )
    return report, bhy_adjusted


def returns_from_trades(trades: Iterable[Any]) -> list[DatedReturn]:
    """Extract (entry_date, pnl_pct) from a list of `strategy.Trade` objects."""
    out: list[DatedReturn] = []
    for t in trades:
        pnl = getattr(t, "pnl_pct", None)
        if pnl is None:
            continue
        out.append((getattr(t, "entry_date", None), float(pnl)))
    return out
