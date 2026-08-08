# Markov Regime Models and GARCH — Research and Application to This Bot

**Written:** 2026-07-25 · **Bot version:** 0.14.0
**Companion to:** [systematic-strategies.md](systematic-strategies.md) — this
document goes deep on two techniques that paper raised at a high level
(§3.3 regime filtering, §3.1 volatility-targeted sizing).

Unlike the companion document, the conclusions here are **backed by tests run on
this bot's own cached data** (`cache/market_data.db`, 2016–2026 daily bars). Both
models were implemented from scratch with scipy rather than installing new
dependencies, so nothing in the venv changed. Scripts are reproducible; see
[Appendix A](#appendix-a--reproducing-these-results).

---

## Bottom line up front

Three findings, in order of how much money/effort they save:

1. **Don't build GARCH.** GARCH(1,1) does beat the ATR the bot currently uses —
   decisively and significantly. But it does **not** significantly beat a
   three-line EWMA (RiskMetrics) estimator (pooled Diebold-Mariano p = 0.12).
   The win is "stop using ATR for sizing," not "adopt GARCH." **Use EWMA.**

2. **The HMM works as a model but loses as a filter.** A 2-state Gaussian HMM
   cleanly separates a calm-bull regime from a turbulent-bear regime on this
   universe, with high persistence — the model is real. But used honestly as an
   entry gate it produced *worse* risk-adjusted returns than a plain 200-day SMA
   filter, and worse than buy-and-hold, on 2018–2026. It does cut drawdown the
   most of any method tested.

3. **The HMM lookahead trap is enormous and worth internalizing.** Scoring the
   same HMM with *smoothed* instead of *filtered* state probabilities inflated
   Sharpe from 0.98 to 2.38 on the bot's universe, and from 0.56 to 2.45 on the
   S&P proxy. That is a 2.4×–4.4× fabricated improvement from a one-line change
   that many published HMM trading examples make. If you take one thing from this
   document, take this.

---

# Part 1 — Markov regime-switching models (HMM)

## 1.1 What the technique is

A Hidden Markov Model treats the market as being in one of several **unobservable
states**, each with its own return distribution, with a fixed probability of
transitioning between them each period. You never observe the state; you infer it
from observable returns. Typically state 1 emits returns with a positive mean and
low variance ("bull/calm"), state 2 a negative or zero mean with high variance
("bear/turbulent").

The appeal over a moving-average filter is that the HMM is **probabilistic and
explicitly models persistence**: it outputs P(regime = bull | data so far) rather
than a binary above/below line, and it learns how sticky each regime is instead
of assuming it.

Your prompt noted these are usually combined with momentum, mean-reversion, or ML
models rather than used standalone — that matches the literature. The standard
applications are: gating an existing signal (allow or enlarge longs only in the
bull state), routing to regime-specific "expert" models, and risk allocation
(shrink exposure as P(turbulent) rises)
([QuantifiedStrategies](https://www.quantifiedstrategies.com/hidden-markov-model-market-regimes-how-hmm-detects-market-regimes-in-trading-strategies/),
[Cube Exchange](https://www.cube.exchange/what-is/market-regime-detection-with-hidden-markov-models)).

**Published evidence** is moderately positive: a regime-switching factor study
over 10.5 years found the HMM-switched model delivered higher absolute returns
than individual factor models out-of-sample from Sept 2017 to Apr 2020
([Regime-Switching Factor Investing with HMMs, *JRFM* 13(12) 311](https://www.mdpi.com/1911-8074/13/12/311)).
QuantStart's QSTrader implementation reports the HMM risk manager helping mainly
by suppressing trades in high-volatility periods
([QuantStart](https://www.quantstart.com/articles/market-regime-detection-using-hidden-markov-models-in-qstrader/)).

## 1.2 What I actually tested

I implemented a 2-state Gaussian HMM (Baum-Welch/EM from scratch) and ran it two
ways on two series — the bot's own universe (equal-weighted NVDA/AMZN/META/AMD)
and an S&P 500 proxy (UPRO daily return ÷ 3, since UPRO is in the cache and SPY
is not).

Protocol, designed to be honest:
- Refit on an **expanding window** every 250 trading days.
- Decide day *t*'s exposure from **filtered** P(state | data through *t*) — the
  forward pass only.
- 750-day burn-in before the first evaluated day.
- The "bull" state is identified each refit as the one with the best mean/σ ratio
  (labels are arbitrary in EM and can switch between fits).

I also computed a deliberately-invalid variant using **smoothed** probabilities
(the forward-backward pass over the full sample) to quantify the lookahead
illusion.

## 1.3 Results

### Bot universe (NVDA/AMZN/META/AMD equal-weight), 1,898 days, Dec 2018 – Jul 2026

| Strategy | CAGR | Vol | Sharpe | Max DD | Time in market |
|---|---:|---:|---:|---:|---:|
| Buy & hold | 45.8% | 37.3% | **1.20** | −60.8% | 100% |
| 200-day SMA filter | 35.9% | 29.1% | **1.20** | −40.4% | 80% |
| **HMM filtered (honest)** | 22.9% | 23.9% | 0.98 | **−24.7%** | 60% |
| *HMM smoothed (lookahead — invalid)* | *46.7%* | *16.7%* | *2.38* | *−11.1%* | *57%* |

### S&P 500 proxy (UPRO ÷ 3), 1,965 days, Dec 2018 – Jul 2026

| Strategy | CAGR | Vol | Sharpe | Max DD | Time in market |
|---|---:|---:|---:|---:|---:|
| Buy & hold | 13.7% | 19.0% | **0.77** | −34.4% | 100% |
| 200-day SMA filter | 8.3% | 11.9% | 0.73 | −22.4% | 78% |
| **HMM filtered (honest)** | 5.1% | 9.9% | 0.56 | **−16.8%** | 62% |
| *HMM smoothed (lookahead — invalid)* | *21.6%* | *8.1%* | *2.45* | *−8.1%* | *68%* |

### The fitted states are genuinely meaningful

The model is not producing noise. On the bot's universe it found:

| State | Mean daily return | Annualized vol | P(stay in state) |
|---|---:|---:|---:|
| Calm / bull | +0.30% | 22.3% | 96.3% |
| Turbulent / bear | −0.01% | 50.5% | 93.9% |

On the S&P proxy: +0.11%/day at 9.4% vol vs −0.08%/day at 28.5% vol, with 97.7%
and 94.4% persistence. That is textbook regime separation — one state has ~2.3×
the volatility and no positive drift. **The model works. The trading application
is what disappoints.**

## 1.4 Interpretation — read this carefully

**The honest HMM lost on Sharpe but won on drawdown.** It cut max drawdown from
−60.8% to −24.7% on the bot's universe (and −34.4% → −16.8% on the S&P proxy),
better than the 200-day SMA managed. It bought that protection by sitting out 40%
of the time, which cost more return than it saved in volatility — hence the lower
Sharpe.

**Three honest caveats that cut in different directions:**

1. *The test window is bull-dominated and therefore biased against every
   defensive overlay.* Dec 2018 – Jul 2026 contains COVID and the 2022 bear, but
   is overwhelmingly a large bull run in exactly these names. Any filter that
   reduces market exposure will look bad here. Note that **buy-and-hold beat both
   filters on Sharpe** — that is a property of the window, not proof that
   filtering is worthless.
2. *Conversely, "reduce drawdown at the cost of Sharpe" is not automatically a
   good trade,* and it should not be dressed up as one. If the goal were minimum
   drawdown, holding less would achieve it more cheaply.
3. *A −60.8% drawdown is what buy-and-hold in this universe actually costs.* For a
   real account, cutting that to −24.7% may be worth a Sharpe point even if the
   ratio says otherwise — Sharpe is symmetric about the mean and does not care
   that drawdowns are what make people abandon systems.

## 1.5 Verdict and recommendation for this bot

**Do not adopt an HMM as an entry gate. Not now, possibly not ever.**

Reasoning specific to this codebase:

- It **lost to a 200-day SMA filter**, which is a fraction of the complexity and
  which [systematic-strategies.md §3.3](systematic-strategies.md) already
  recommends implementing first. Do the simple thing, measure it, and only reach
  for the HMM if the simple thing proves insufficient.
- It adds a **fitted, refitted, stochastic component** to a system that
  [§3.7 of the companion doc](systematic-strategies.md) already identifies as
  having no held-out validation and no trial logging. Adding an EM-fitted latent
  variable model to an unvalidated research loop is the wrong sequencing.
- EM has **label-switching and local-optimum instability**. My implementation
  re-identifies the bull state at each refit by mean/σ; a naive implementation
  that assumes "state 0 = bull" will silently invert and trade backwards.

**If you revisit it later**, the defensible use is not a binary gate but a
**continuous risk scalar**: size positions by P(calm regime) instead of turning
trading on and off. That is the "combine with, don't replace" pattern your prompt
described, it degrades gracefully when the model is uncertain, and it composes
naturally with the volatility-targeted sizing in Part 2.

## 1.6 The lookahead trap — the most transferable lesson here

Smoothed probabilities, P(state_t | **all** data), condition on the future. They
are the right tool for *explaining* history and completely invalid for
*backtesting*. Filtered probabilities, P(state_t | data through *t*), are what a
live system can compute.

The measured cost of confusing them, on identical models and data:

| Series | Honest (filtered) Sharpe | Lookahead (smoothed) Sharpe | Inflation |
|---|---:|---:|---:|
| Bot universe | 0.98 | 2.38 | **2.4×** |
| S&P proxy | 0.56 | 2.45 | **4.4×** |

A Sharpe of 2.4 with an −11% max drawdown would look like a career-making
discovery. It is an artifact of one function call. Many blog-tier HMM trading
tutorials fit on the full sample and plot the smoothed states — treat any HMM
result without an explicit walk-forward/filtered protocol as unproven.

---

# Part 2 — GARCH

## 2.1 What the technique is

GARCH(1,1) models conditional variance as

> σ²_t = ω + α·r²_{t−1} + β·σ²_{t−1}

Tomorrow's variance is a weighted blend of a long-run average (ω), yesterday's
surprise (α·r²), and yesterday's variance (β·σ²). It captures **volatility
clustering** — the empirical fact that turbulent days follow turbulent days — and
mean-reverts toward the long-run level at a rate set by the persistence α+β.

It is the industry standard for volatility forecasting in banks and risk systems,
and the standard trading application is exactly what
[§3.1 of the companion doc](systematic-strategies.md) proposes: reduce exposure
when forecast volatility rises, increase it when calm is predicted
([V-Lab / NYU Stern](https://vlab.stern.nyu.edu/docs/volatility/GARCH),
[Portfolio Optimizer](https://portfoliooptimizer.io/blog/volatility-forecasting-garch11-model/)).

The literature is notably unsettled on whether GARCH beats simpler estimators in
practice. EWMA has been found to outperform GARCH and stochastic-volatility models
over 1–5 month horizons in moderate-volatility periods, and hybrid realized-EWMA
estimators beat both when high-frequency data is available
([comparison overview](https://metricgate.com/blogs/garch-vs-ewma-vs-realized-volatility/),
[Realized GARCH international evidence](https://www.sciencedirect.com/science/article/abs/pii/S1062976915000800)).
That disagreement is precisely why I tested it here rather than assuming.

## 2.2 What I actually tested

A five-way out-of-sample forecast horse race on each of NVDA, AMZN, META, AMD
(1,898 evaluated days each, ~7.5 years):

| Model | Description |
|---|---|
| `garch` | GARCH(1,1) by maximum likelihood, refit every 250 days on an expanding window |
| `ewma94` | RiskMetrics EWMA, λ = 0.94 — no fitting at all |
| `sd60` | Trailing 60-day standard deviation |
| `sd20` | Trailing 20-day standard deviation |
| `atr14` | **What the bot uses today** — Wilder ATR(14), scaled to a variance |

Every forecast for day *t* uses only data through *t−1*. The ATR series was
calibrated to a comparable variance scale on the burn-in period only (then held
fixed), so the comparison is fair rather than rigged against it.

Scored with **QLIKE** = log(σ̂²) + r²/σ̂², which is robust to the noise in using
squared returns as a realized-variance proxy. MSE is also reported but is *not*
the right metric here — it is dominated by a handful of extreme days and gives
inconsistent rankings, which the results show.

## 2.3 Results

### Mean QLIKE, pooled across all four tickers (lower is better)

| Model | Mean QLIKE | Avg rank | Verdict |
|---|---:|---:|---|
| **garch** | **−6.1766** | 1.00 | Best on all 4 tickers |
| **ewma94** | −6.1533 | 2.00 | Second on all 4 |
| sd60 | −6.1195 | 3.25 | |
| sd20 | −6.0612 | 3.75 | |
| **atr14** | **−5.6491** | 5.00 | **Worst on all 4 — this is what the bot uses** |

### Diebold-Mariano tests (Newey-West HAC), pooled

| Comparison | DM stat | p-value | Conclusion |
|---|---:|---:|---|
| garch vs ewma94 | −1.55 | **0.121** | **NOT significant** |
| ewma94 vs atr14 | −8.64 | <0.0001 | EWMA better, overwhelmingly |
| garch vs atr14 | −7.84 | <0.0001 | GARCH better, overwhelmingly |
| ewma94 vs sd60 | −2.45 | 0.014 | EWMA better |
| garch vs sd20 | −4.07 | <0.0001 | GARCH better |

Per-ticker, GARCH beat EWMA significantly on only **1 of 4** names (AMZN,
p = 0.036); the other three were coin flips (p = 0.45, 0.37, 0.95). Meanwhile
EWMA beat ATR at p < 0.0001 on **all four**.

Fitted GARCH persistence (α+β) ranged 0.75–0.95, consistent with genuine
volatility clustering in these names.

## 2.4 Interpretation

**The gap that matters is ATR → anything, not EWMA → GARCH.** In QLIKE units, the
ATR-to-EWMA improvement is ~0.50; the EWMA-to-GARCH improvement is ~0.023. EWMA
captures roughly **96% of the total available improvement** at approximately 1% of
the implementation complexity — no optimizer, no refit schedule, no convergence
failures, no multiple-restart logic, three lines of code.

**Why ATR does so badly:** ATR is a mean-absolute-range measure designed for
*placing stop levels*, not a conditional variance forecast. It ignores overnight
gaps' contribution to squared returns, responds to range rather than to squared
deviation, and its Wilder smoothing is a fixed slow decay that cannot adapt. It is
a fine tool for the job it currently does in this bot (setting TP/SL geometry) and
a poor tool for the job §3.1 proposed giving it (sizing).

**Honest caveat on metric choice:** on MSE the ranking is not clean — GARCH is
*worse* than sd60 on META (10.03 vs 9.30), and ATR sometimes scores well. This is
expected and is why QLIKE is the standard choice: MSE against a squared-return
proxy is dominated by outliers and is not a consistent ranking criterion for
variance forecasts. I am reporting the metric I believe is correct, not the one
that looks tidiest, and flagging that the two disagree.

## 2.5 Verdict and recommendation for this bot

**Adopt EWMA volatility. Do not build GARCH.**

Concretely, this changes [§3.1 of the companion doc](systematic-strategies.md)
from "use ATR for vol targeting" to "use EWMA for vol targeting" — a strictly
better recommendation that costs nothing extra.

### Where it plugs in

**(a) Volatility-targeted position sizing** — the main application.

- Add to `StrategyParams` in `config.py`: `vol_target_annual_pct`,
  `vol_ewma_lambda` (0.94), `vol_scalar_min` / `vol_scalar_max` (clamps).
- Compute EWMA variance per ticker in `strategies/base.py` alongside the existing
  indicators — it belongs next to `atr()` and is about as long:

  ```python
  def ewma_var(close: pd.Series, lam: float = 0.94) -> pd.Series:
      """RiskMetrics conditional variance. Shift before use: var[t] must not
      see r[t]."""
      r2 = close.pct_change() ** 2
      return r2.ewm(alpha=1 - lam, adjust=False).mean()
  ```

  Expose it from `add_indicators` as `ewma_vol` (= √(252·var)) so backtests and
  live share one definition, exactly as `atr_pct` is shared today.
- In `position_sizing.whole_share_position_size`, accept a `vol_scalar` and use
  `budget = equity * fraction * clamp(target_vol / forecast_vol, lo, hi)`.
  **The clamp is not optional** — an unclamped inverse-vol rule takes enormous
  positions in a temporarily quiet name.
- Thread it through both `bot.py`'s live sizing path and
  `backtest_portfolio.run_annual_portfolio`, matching the discipline already used
  for the leveraged cap.

**(b) A volatility-scaled daily-loss kill switch** — a smaller, cheaper win
specific to this bot.

`max_daily_loss_pct = 0.03` is currently regime-blind. Given the fitted state
volatilities above (22% annualized in calm, 51% in turbulent), a 3% daily move is
a ~2.1σ event in the calm regime and a ~0.9σ event in the turbulent one. The same
threshold therefore means completely different things: near-unreachable in calm
markets, and tripping on ordinary noise in volatile ones — halting entries on days
that are not actually unusual. Scaling the threshold by EWMA volatility (floored
at the current 3% so it can only ever become *more* conservative) makes the kill
switch mean one consistent thing. This touches only the kill-switch check in
`bot.py` and its mirror in the backtest.

**(c) Not recommended: replacing ATR in TP/SL geometry.** ATR is doing the right
job there — it is a range measure and stop placement is a range problem. Changing
it would invalidate every existing backtest for no established benefit.

---

# Part 3 — Combining them, and where they sit in the roadmap

**Markov-switching GARCH** (a GARCH whose parameters switch by hidden regime) is
the natural union of Parts 1 and 2, and it appears in the value-at-risk literature
— e.g. Bayesian Markov-switching GJR-GARCH copula-EVT models for VaR refinement
([PMC](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC6014648/)).

**It is emphatically not appropriate for this bot.** Given that plain GARCH could
not significantly beat a three-line EWMA on this data, and the HMM gate lost to a
200-day moving average, a model combining both would add many fitted parameters to
capture an effect that was not measurable in either component alone. It is the
kind of model whose in-sample backtest would look excellent for reasons
[§3.7 of the companion doc](systematic-strategies.md) explains in detail.

### Revised priority (updating the companion document's table)

| # | Item | Change from companion doc |
|---|---|---|
| 1 | Validation methodology (§3.7) | Unchanged — still first |
| 2 | Index regime filter, 200-day SMA (§3.3) | **Confirmed as the right choice over the HMM** — but see [bear-markets-and-crashes.md](bear-markets-and-crashes.md), which tested this window's blind spot (2000–2002, 2007–2009, neither reachable from the Alpaca cache) and found "drawdown ≥10% off the 52-week high" beats the 200-day SMA on retained return, max-drawdown cut, and whipsaw frequency across all four real ≥20% bears since 1990. Worth an A/B test before shipping either. |
| 3 | Volatility-targeted sizing (§3.1) | **Use EWMA, not ATR** — measured here |
| 3b | Vol-scaled kill switch | **New** — cheap, follows from the same EWMA series |
| 4 | Correlation-aware caps (§3.2) | Unchanged |
| — | GARCH | **Rejected** — not significantly better than EWMA |
| — | HMM entry gate | **Rejected for now** — lost to a 200-day SMA |
| — | HMM as continuous risk scalar | Optional, only after 1–4 land |

---

## Pitfalls specific to these two techniques

- **Filtered vs smoothed probabilities (HMM).** Measured here to inflate Sharpe
  2.4×–4.4×. Never backtest on smoothed states.
- **Label switching (HMM).** EM assigns state indices arbitrarily and they can
  swap between refits. Always re-identify the bull state by a property (mean/σ),
  never by index.
- **Shifting the variance series (both).** `var[t]` must be computed from returns
  through `t−1`. An EWMA that includes today's return in today's forecast will
  look excellent and is lookahead. This is the same class of bug the project
  already guards against with `data_feed.completed_bars`.
- **Unclamped inverse-vol sizing.** A quiet stretch produces a tiny volatility
  estimate and an enormous position. Always clamp the scalar.
- **GARCH convergence.** The likelihood is not well-behaved from arbitrary starts;
  my implementation needed four restarts and a fallback. This fragility is part of
  the argument for EWMA.
- **Bull-dominated evaluation windows.** 2018–2026 flatters buy-and-hold and
  penalizes every defensive overlay. Any regime work should be evaluated on
  bear-heavy periods too — the 2016+ cache helps, but a genuine test wants
  2000–2002 and 2007–2009, which this cache does not cover.

---

## Appendix A — Reproducing these results

Three standalone scripts were written for this analysis (in the session
scratchpad, not committed — they depend only on `cache/market_data.db`, numpy,
scipy, and pandas, all already present):

| Script | Produces |
|---|---|
| `vol_race.py` | The five-model QLIKE/MSE horse race in §2.3 |
| `dm_test.py` | Diebold-Mariano significance tests with Newey-West errors |
| `hmm_regime.py` | Baum-Welch HMM, filtered vs smoothed comparison in §1.3 |

If this work is taken further, they should be moved into `tests/` or a
`research/` directory and wired into the experiment log
(`db_mod.log_experiment`), per §3.7 of the companion document — every
configuration tried needs recording, not just the ones that worked.

---

## Sources

**Markov / HMM**
- [HMM Market Regimes — QuantifiedStrategies](https://www.quantifiedstrategies.com/hidden-markov-model-market-regimes-how-hmm-detects-market-regimes-in-trading-strategies/)
- [Market Regime Detection with HMMs — Cube Exchange](https://www.cube.exchange/what-is/market-regime-detection-with-hidden-markov-models)
- [Regime-Switching Factor Investing with Hidden Markov Models — *JRFM* 13(12) 311](https://www.mdpi.com/1911-8074/13/12/311)
- [Market Regime Detection using HMMs in QSTrader — QuantStart](https://www.quantstart.com/articles/market-regime-detection-using-hidden-markov-models-in-qstrader/)
- [Regime Detection and Risk Allocation Using HMMs — Bocconi BSIC](https://bsic.it/regime-detection-and-risk-allocation-using-hidden-markov-models/)

**GARCH / volatility forecasting**
- [GARCH Volatility Documentation — V-Lab, NYU Stern](https://vlab.stern.nyu.edu/docs/volatility/GARCH)
- [Volatility Forecasting: GARCH(1,1) — Portfolio Optimizer](https://portfoliooptimizer.io/blog/volatility-forecasting-garch11-model/)
- [GARCH vs EWMA vs Realized Volatility for Risk Modeling — Metricgate](https://metricgate.com/blogs/garch-vs-ewma-vs-realized-volatility/)
- [Volatility Estimation: EWMA and GARCH Explained — Ryan O'Connell, CFA](https://ryanoconnellfinance.com/volatility-estimation-garch/)
- [Forecasting stock market volatility using Realized GARCH: International evidence — ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S1062976915000800)
- [Volatility Forecasting — A Comparison of GARCH(1,1) and EWMA models](https://www.researchgate.net/publication/280545501_Volatility_Forecasting_-_A_Comparison_of_GARCH11_and_EWMA_models)
- [Refining VaR estimates using a Bayesian Markov-switching GJR-GARCH copula-EVT model — PMC](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC6014648/)
