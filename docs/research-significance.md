# Research significance and F08 revalidation

As of v0.24.13, finite nonconstant samples retain the ordinary one-sided
one-sample statistic `t = mean / (sample_std / sqrt(n))`. There is no `sqrt(n)`
upper bound. The alternative is mean return greater than zero, so negative
statistics have large one-sided p-values. This follows the
[SciPy one-sample t-test definition](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.ttest_1samp.html).

## Samples and bootstrap policy

- Missing and nonfinite returns are excluded before counting observations.
- A single `evaluate` calculation needs at least two finite, nonconstant
  observations. Exact constants, insufficient observations and numerically
  unrepresentable statistics use `(t=0, p=1)` as a no-evidence convention.
  This is not a mathematical test result for a zero-variance population.
- Small **nonzero** variance is retained. Rescaling protects the statistic
  from variance underflow/overflow without imposing an arbitrary t cutoff.
  Sharpe uses the same calculation, report means are computed safely, and
  bootstrap originals are scaled before centering so a finite input cannot
  become an infinite residual and then silently contribute a zero tail.
- Original variants used to calibrate a search need at least 20 finite trades,
  at least two calendar months, and a representable, nonconstant sample. These
  are minimum eligibility rules, not evidence that two months are sufficient
  for a reliable trading conclusion. `min_trades` can be explicitly configured
  on the low-level bootstrap function, with a minimum of two.
- Calendar months are drawn jointly across eligible variants. Repeated draws
  of the same month are retained. Each eligible variant is demeaned before
  resampling. Ineligible variants do not establish the bootstrap maximum, but
  remain in `evaluate_search`'s trial count and BHY p-value panel; an ineligible
  winner cannot use another variant's calibrated hurdle to claim significance.
- In a resample, an eligible variant with at least two trades contributes its
  t-statistic even below the original 20-trade floor (small-sample t values are
  heavy-tailed, so thin variants raise the hurdle honestly); a variant with
  fewer than two trades has no statistic in that resample, and a resample in
  which no variant has one receives an infinite upper bound. (Until v0.25.1 any
  variant below 20 resampled trades made the whole draw infinite, so realistic
  panels always reported an infinite hurdle and nothing could pass.)
  A constant positive null resample likewise receives positive infinity;
  constant nonpositive resamples contribute zero to the positive maximum.
  Other numerically unrepresentable resamples receive positive infinity.
  Draws are neither discarded nor retried. This conservative policy prevents
  untestable draws from silently shrinking the upper tail.
- The hurdle uses the upper neighboring empirical order statistic
  (`numpy.quantile(..., method='higher')`), avoiding interpolation between
  infinities. See [NumPy's quantile methods](https://numpy.org/doc/stable/reference/generated/numpy.quantile.html).
  An infinite hurdle or an ineligible winner is reported as uncalibrated and
  cannot clear the search. `n_boot` must be a positive integer and
  `0 < alpha < 1`.

Observed-sample degeneracy and null-resample degeneracy intentionally have
different conventions: refusing evidence from a constant observed sample must
not erase a positive bootstrap tail. Large finite tail statistics remain in the
maximum distribution, even when they make the hurdle much larger.

## Revalidating old research

Old significance verdicts are not validated by this code correction. Both
observed statistics and bootstrap hurdles can change, in either direction.
The fix does not resolve overlapping returns, reused history, selection of the
stock universe, or the remaining execution/data/optimizer findings.

To revalidate an existing search:

1. Recover every variant's dated fractional returns, including losing variants,
   the originally selected winner, and the full historical trials count.
   Keep the original dataset and parameter definitions fixed.
2. Call `evaluate_search(configs, winner=original_winner, alpha=original_alpha,
   n_boot=original_n_boot, seed=original_seed)` using the corrected code.
   Record excluded/uncalibrated cases rather than relabeling them as passed.
3. Save the corrected report alongside the original, together with code
   revision, data provenance and bootstrap settings. Do not overwrite the
   original evidence or silently discard earlier losing trials.
4. Treat rerunning a backtest with corrected fills/data as a separate baseline
   regeneration; it changes the underlying returns as well as the statistic.

Repository inspection on 2026-09-14 found no complete archived optimizer search
panels to replay. The optimizer returns per-variant observations in memory but
does not persist them; the research-experiment database contains no rows. The
saved historical JSON contains cumulative/yearly summaries rather than full
optimizer search panels. Therefore no historical significance verdict has been
revalidated or rewritten in this release. Synthetic panels, including losing
variants and degenerate draws, are covered by regression tests.
