# Systematic Trading Strategies — Research Notes & Application to This Bot

**Written:** 2026-07-25 · **Bot version at time of writing:** 0.14.0

This document surveys techniques from the systematic-trading literature and
assesses which ones are worth adding to *this* bot, given what it already does.
It is a research note, not an implementation plan — nothing here has been
backtested on this system yet. Every proposal ends with the specific file and
function that would change, plus an honest statement of what it does *not* fix.

---

## Part 1 — What this bot already is, in systematic-trading terms

It is worth naming the current design precisely, because it determines which
techniques are additive and which are redundant.

All eight strategies in `strategies/` are **long-only, time-series (absolute)
signals on a five-name universe**, sized by a **fixed fraction of equity**, with
**broker-held bracket exits**.

| Dimension | Current design |
|---|---|
| Direction | Long only — every entry is `OrderSide.BUY` (`bot.py:183`, `bot.py:217`) |
| Signal type | Time-series / absolute: each ticker is judged against its own history, never ranked against the others |
| Universe | 5 names (`config.TICKERS`) + TQQQ scoped to one strategy (`config.LEVERAGED_TICKERS`) |
| Sizing | Fixed 20% of equity, whole shares (`position_sizing.whole_share_position_size`) |
| Portfolio limit | Count-based: 5 concurrent positions (`max_concurrent_positions`) |
| Exit | Fixed bracket (ATR-scaled TP + % SL), or signal exit for `sma_50_cross` / `tqqq_momentum` |
| Risk overlay | Daily-loss kill switch (−3%), entry slippage guard, leveraged-notional cap |

The eight strategies are **less diversified than the count suggests**. Six of
them (`trend_pullback`, `breakout`, `mean_reversion`, `momentum_macd`, `regime`,
`ensemble`) are variations on "is this name trending or bouncing right now,"
computed from overlapping indicators (`add_indicators` in `strategies/base.py`
feeds all of them from the same SMA/RSI/ATR/MACD/Bollinger set). `ensemble` is
explicitly a weighted vote of five of the others (`strategies/ensemble.py:16`),
so it is a *combination* of existing bets, not a new one.

The genuinely distinct edges in the repo today are: **trend-following**
(`sma_50_cross`, `tqqq_momentum` — ride until the trend breaks) and
**short-horizon mean-reversion** (`mean_reversion`). Everything else sits
between those two poles.

**Implication:** the highest-value additions are not more entry signals of the
same family. They are the dimensions the bot has *none* of — position sizing
that responds to risk, portfolio construction that sees correlation, a
market-level regime switch, and a validation methodology that can tell a real
edge from a fitted one.

---

## Part 2 — Measured diagnostics (not assumptions)

Before recommending anything, I measured the actual universe using the cached
daily bars in `cache/market_data.db` (2016–2026, 2,648 observations for the four
long-history names; ARM only lists from 2023).

### The universe is one bet wearing five hats — and it gets worse under stress

Mean pairwise daily-return correlation:

| Regime | Mean pairwise correlation | Effective independent bets (5 positions) |
|---|---|---|
| Calm 2017 | 0.41 | ~1.9 |
| Full sample 2016–2026 | 0.50 | ~1.7 |
| Low-vol regime (bottom 20% trailing vol) | 0.35 | ~2.1 |
| High-vol regime (top 20% trailing vol) | **0.66** | **~1.4** |
| 2022 bear market | 0.68 | ~1.4 |
| **COVID crash (Feb–Apr 2020)** | **0.86** | **~1.1** |

*Effective independent bets = 1 / (1/n + ((n−1)/n)·ρ) for n equal-weighted
positions — the factor by which portfolio variance is actually reduced.*

Two things follow directly:

1. **A "5 positions max" limit does not mean five bets.** At the historical
   average correlation of 0.50, five full positions behave like ~1.7 independent
   positions. The diversification the position count implies mostly is not there.
2. **The diversification evaporates exactly when it is needed.** In the COVID
   crash the five names behaved as ~1.1 independent bets — effectively a single
   levered position in "megacap tech." This is the textbook correlation-breakdown
   effect, and it is measurable in this specific universe, not a general warning.

Note on methodology: my first attempt measured correlation on the worst-decile
*return* days and appeared to show correlation *falling* under stress (0.13).
That was a selection-bias artifact — conditioning on a linear combination of the
variables truncates the sample and biases correlation toward zero. The table
above conditions on *trailing* volatility (known in advance, no lookahead) and
on calendar periods instead, which is the correct approach.

### What this means for the existing risk controls

The `max_leveraged_exposure_pct` cap (added in 0.14.0) was built on exactly this
reasoning — the CHANGELOG notes the position limit "is count-based, so it cannot
see correlation." That logic is correct and the measurement above confirms it,
but it was applied only to the leveraged-ETF group. **The same blindness applies
to the core five-name universe**, which is empirically ~0.5 correlated and ~0.66
in stress. The existing cap is the right idea scoped too narrowly.

---

## Part 3 — Candidate techniques

Ordered by my estimate of value-per-unit-effort for this specific system.

---

### 3.1 Volatility-targeted position sizing

**The idea.** Instead of a fixed fraction of equity per position, size each
position inversely to its recent volatility, so every position contributes
roughly equal *risk* rather than equal *dollars*. Position size ∝ target_vol /
recent_vol, usually estimated from ATR or trailing realized standard deviation.

**Evidence.** Volatility targeting is one of the better-documented improvements
in systematic trading. Reported results include Sharpe rising from 0.99 to 1.54
and max drawdown falling from −30.8% to −13.8% when moving from equal weighting
to inverse-volatility weighting on rolling 12-month standard deviation
([QuantPedia](https://quantpedia.com/an-introduction-to-volatility-targeting/)).
The mechanism is not return prediction — it is that volatility is strongly
autocorrelated (calm periods follow calm periods), so scaling exposure down in
turbulent periods genuinely stabilizes portfolio volatility and cuts tail risk
([Rob Carver, "Vol targeting and trend following"](https://qoppac.blogspot.com/2018/07/vol-targeting-and-trend-following.html)).

**Fit here — strong.** This bot currently gives NVDA at 60% annualized
volatility and AMZN at 30% the *same* 20% of equity, which means NVDA
contributes roughly twice the risk. Worse, the interaction with whole-share
rounding is arbitrary: a $538 share of one name and a $205 share of another get
wildly different effective risk per slot. The bot already computes ATR for every
bar (`strategies/base.py:atr`, exposed as `atr_pct` in `add_indicators`), so the
volatility estimate is **already sitting in the dataframe, unused for sizing**.

**Implementation sketch.**
- Add `target_position_vol_pct` and `vol_sizing_enabled` to `StrategyParams`
  (`config.py`).
- In `position_sizing.whole_share_position_size`, accept an optional
  `vol_scalar` and compute `budget = equity * fraction * clamp(target_vol /
  recent_vol, min_scale, max_scale)`. Clamping matters — an unclamped inverse-vol
  rule takes enormous positions in a temporarily quiet name.
- Feed `atr_pct` from the signal bar through `EntrySignal` (it already carries
  `atr`) into both the live sizing path (`bot.py`, around the existing
  `whole_share_position_size` call) and `run_annual_portfolio`
  (`backtest_portfolio.py`), so backtest and live stay identical — the project
  already enforces this discipline for the leveraged cap.

**What it does not fix.** It does not improve entry timing and it does not help
if all positions are correlated — a vol-targeted portfolio of five correlated
names is still ~1.7 bets. Pair it with 3.2.

**Effort:** moderate. **Risk:** low — it is a sizing change, testable against
existing backtests with the signal set held constant.

---

### 3.2 Correlation-aware portfolio limits (generalize the leveraged cap)

**The idea.** Replace or supplement the count-based position limit with a
constraint on *aggregate risk*: cap total portfolio volatility, or cap exposure
per correlated cluster, rather than counting slots.

**Evidence.** The standard practical threshold is that correlations above ~0.7
indicate a portfolio is effectively concentrated rather than diversified, and
the well-documented failure mode is that correlations spike in crises precisely
when diversification is being relied upon
([Saxo](https://www.home.saxo/learn/guides/diversification/how-correlation-impacts-diversification-a-guide-to-smarter-investing),
[Britannica Money on concentration risk](https://www.britannica.com/money/concentration-risk-management)).
Risk-parity construction — allocating by risk contribution rather than dollars —
is the standard response.

**Fit here — strong, and the codebase already has the pattern.**
`leveraged_headroom()` in `position_sizing.py` is exactly a group-level exposure
cap; it just only knows about one group. The measured 0.66 stress correlation
across the core universe says the core five deserve the same treatment.

**Implementation sketch.**
- Simplest version, no correlation estimation needed: add a
  `max_sector_exposure_pct` and a static cluster map in `config.py` (all four
  semis/megacap-tech names are one cluster today). Reuse `leveraged_headroom()`
  by generalizing it to `group_headroom(equity, open_notional, cap_fraction)` —
  the function is already group-agnostic in everything but its name.
- Richer version: maintain a rolling correlation matrix from `market_cache`, and
  before each entry compute the marginal contribution to portfolio volatility;
  reject entries that push estimated portfolio vol past a ceiling.
- Preserve the existing **fail-closed** discipline from `_open_leveraged_notional`
  (an unreadable position is charged the full cap) — that is the right default
  and should carry into any generalized version.

**What it does not fix.** It reduces exposure but does not add a genuinely
uncorrelated return stream. The real fix for a one-bet universe is a wider,
more heterogeneous universe (3.6).

**Effort:** low for the static-cluster version. **Risk:** low.

---

### 3.3 Index-level regime filter (the bear-market gap)

**The idea.** Gate *all* new entries on the state of the broad market, not on
per-ticker signals: trade only when the index is above its long-term trend
(commonly the 200-day SMA), stand aside otherwise. This is "absolute momentum"
or the trend/regime filter in dual-momentum systems.

**Evidence.** Siegel's work bought the DJIA when it closed ≥1% above its 200-day
MA and sold when ≥1% below, finding improved absolute and risk-adjusted returns
and — critically — avoidance of the 1929–32 collapse. Antonacci's dual-momentum
work finds absolute momentum does more to reduce volatility and drawdown than
relative momentum does, and Clenow's momentum system uses an index 200-day MA
filter to decide when to hold equities at all
([overview](https://www.crackingmarkets.com/us-stock-momentum-trading-system-for-retail-traders-deep-research/),
[Quantpedia: Asset Class Trend-Following](https://quantpedia.com/strategies/asset-class-trend-following)).
Independent testing of ~20 trend-based regime filters across four bear markets
confirms regime filtering works, while noting many commercial implementations
overcharge for it
([setup4alpha](https://setup4alpha.substack.com/p/i-tested-20-trend-based-regime-filters)).

**Fit here — this is the specific hole identified earlier.** The bot's only
downside defense is the −3% daily kill switch, which **re-arms every morning**.
In a grinding multi-week decline it disengages for a day and comes back. Given
that the longest historical S&P 500 bear market ran ~996 days (1929–32) and the
average is ~406 days, a one-day halt is not a bear-market defense.

**Important nuance — this is not the experiment that already failed.**
`program.md` logs a rejected experiment (2026-06-01): a VIX < SMA(20) filter
applied to the Breakout strategy, which worsened both years and was reverted.
That is a *different* intervention: it was a volatility filter, applied to one
strategy, at the signal level. What is proposed here is an *index trend* filter
applied at the **portfolio level** as a global entry gate. The prior negative
result does not carry over, but it is a fair warning that regime filters are
easy to get wrong on a five-name tech universe.

**Implementation sketch.**
- Fetch SPY (or QQQ, given the universe's character) daily bars via the existing
  `market_cache` — no new data source needed.
- Add a `regime_ok()` check in `bot.run_once` alongside the existing kill-switch
  check, gating new entries only. Exits and broker-held protection must keep
  running unconditionally, matching how `max_daily_loss_pct` already behaves.
- Mirror it in `run_annual_portfolio` so backtests reflect it.
- Test the band (the ±1% buffer in Siegel's rule exists to suppress whipsaw
  around the line) and test QQQ vs SPY as the reference index.

**What it does not fix.** It keeps the bot out of trouble; it does not make
money in a downturn. Given the bot is long-only by deliberate design, "sit in
cash" is the correct ambition here — not shorting.

**Effort:** low. **Risk:** moderate — regime filters reduce drawdown but usually
also reduce total return, and they can whipsaw. Must be judged on risk-adjusted
return, not P&L.

---

### 3.4 Cross-sectional momentum (ranking, not just screening)

**The idea.** Rather than asking "is NVDA trending?" independently per name, rank
the universe and hold only the strongest. Cross-sectional momentum is relative;
the bot's current signals are all absolute.

**Evidence.** Mixed, and worth stating honestly. Cross-sectional momentum is the
dominant form in the asset-pricing literature, but Moskowitz et al. (2012) found
time-series momentum performed well both absolutely and *relative to*
cross-sectional momentum across 24 markets, and one comprehensive study found
time-series momentum "clearly superior" across markets. Results vary by asset
class — cross-sectional won in some currency studies. Recent panel regressions
with country and month fixed effects find only weak evidence of time-series
momentum in global stock markets
([QuantPedia comparison](https://quantpedia.com/time-series-vs-cross-sectional-implementation-of-momentum-value-and-carry-strat/),
[Bird, Gao & Yeung 2017](https://journals.sagepub.com/doi/10.1177/0312896215619965)).

**Fit here — currently weak, but it unlocks later.** With five names, ranking is
nearly meaningless: the cross-section is too small for the tails (where momentum
information concentrates) to exist. Cross-sectional momentum becomes worthwhile
only alongside universe expansion (3.6). It would, however, cleanly solve a
problem the bot handles arbitrarily today: **when more signals fire than there
are slots, which do you take?** Right now that is resolved by iteration order
over `TICKERS` — effectively alphabetical luck, not a decision.

**Implementation sketch.** Even before expanding the universe, replace
first-come-first-served slot allocation with a ranked queue: collect all
candidate signals in a cycle, score them (e.g. 12-1 month return, or signal
strength — `ensemble` already produces a numeric score, embedded in its strategy
label at `strategies/ensemble.py:69`), and fill slots best-first. That is a small
change to the candidate loop in `bot.run_once` and to
`collect_backtest_candidates`.

**Effort:** low for ranked slot allocation, high for true cross-sectional
momentum. **Risk:** low for the former.

---

### 3.5 Meta-labeling (an ML layer that sizes, not one that predicts direction)

**The idea.** Keep the existing strategies as the *primary* model deciding trade
direction, and train a *secondary* binary classifier that predicts whether each
signal will be profitable. Use it to filter out false positives and to size
positions by confidence. It separates "what to trade" from "whether to trade."

**Evidence.** Developed by López de Prado (2017). It is designed to improve
precision without sacrificing recall, which is the right trade when a primary
model has high recall and too many false positives — yielding higher F1 and
Sharpe ([Wikipedia](https://en.wikipedia.org/wiki/Meta-Labeling),
[Hudson & Thames](https://hudsonthames.org/does-meta-labeling-add-to-signal-efficacy-triple-barrier-method/)).
It is explicitly **not** a silver bullet — practitioner writeups document cases
where it adds nothing ([QuantConnect discussion](https://www.quantconnect.com/forum/discussion/14706/why-meta-labeling-is-not-a-silver-bullet/)).

**Fit here — conceptually excellent, practically premature.** The bot's
architecture is unusually well-suited: `ensemble` is already a hand-weighted
primary model whose weights were tuned by hand in a single pass
(`strategies/ensemble.py:13-22`), and the bracket exit structure (fixed TP/SL +
time stop) is *precisely* the triple-barrier labeling scheme meta-labeling
assumes. The labels are essentially free — `dashboard/db.py` already records
every trade's outcome and exit reason.

**The blocker is sample size.** The DB currently holds 39 live trades, and the
backtests produce on the order of 20–60 trades per strategy-year. Training a
classifier on a few hundred labels, from one five-name universe, over three
years that were all bull markets, would produce a model that memorizes 2024–2026
megacap tech. The honest prerequisite is thousands of labeled events — which
means the 2016–2026 history already in `cache/market_data.db` (10 years, and
`backtest_history.py` already runs over it), a wider universe, and the
validation machinery in 3.7.

**Effort:** high. **Risk:** high — this is the single easiest way to add a
convincing-looking overfit model to this system. Defer until 3.7 exists.

---

### 3.6 Universe expansion

**The idea.** Trade more, and more heterogeneous, instruments.

**Fit here — this is the precondition for several of the above.** The measured
0.50 average / 0.66 stress correlation is a direct consequence of holding four
semiconductor-and-megacap-tech names plus one. Cross-sectional momentum (3.4)
needs breadth to rank; meta-labeling (3.5) needs sample count; correlation caps
(3.2) only bind if there is somewhere else to deploy capital.

Notably, `cache/market_data.db` **already holds 10 years of 4h bars for TQQQ,
SOXL, TECL, FAS, LABU, TNA, UDOW, and UPRO** — from the leveraged-ETF research
recorded in CHANGELOG 0.14.0 (all 8 profitable, median PF 1.66, median max DD
5.8%). The data cost of expansion has already been paid.

**The caution.** Expanding into more 3x leveraged ETFs increases breadth in name
count while *decreasing* it in risk terms — they track correlated underlyings and
their signals fire together, which is exactly the scenario
`max_leveraged_exposure_pct` was built to contain. Genuine diversification means
instruments with different drivers, not more expressions of "US tech goes up."

**Effort:** low mechanically (`config.TICKERS` plus a backtest run), high to do
*well*. **Risk:** moderate — every added name multiplies the number of
strategy/ticker combinations tested, which inflates selection bias (3.7).

---

### 3.7 Validation methodology — the highest-value item in this document

**The idea.** Adopt walk-forward analysis, purged cross-validation, and
selection-bias-corrected performance statistics, instead of "keep the change if
both backtest years improve."

**Evidence.** Bailey and López de Prado proved that high simulated performance is
easily achievable after testing relatively few strategy configurations, and that
memory effects in financial series cause overfit strategies to *systematically*
underperform out-of-sample — not merely to disappoint. Their **Deflated Sharpe
Ratio** corrects for selection bias under multiple testing and non-normal
returns ([SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551)).
**Purged cross-validation** removes training observations whose label horizons
overlap the test set, preventing lookahead leakage
([overview](https://en.wikipedia.org/wiki/Purged_cross-validation)). Comparative
work finds **Combinatorial Purged CV** superior to K-Fold, Purged K-Fold, and
especially Walk-Forward at controlling the Probability of Backtest Overfitting
([ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S0950705124011110)).

**Fit here — this is the most important gap in the entire project.** The
research loop documented in `program.md` and `CLAUDE.md` is:

> 3. Compare in dashboard or DB; keep if both years improve

Read against the literature, that procedure is a selection-bias generator. The
evidence is in the project's own logs:

- `program.md` records the ensemble weights being retuned until 2025 went from
  −$28.15 to +$194.44 — a fivefold swing produced by adjusting five weights,
  scored on the same two years used to choose them.
- The ensemble threshold was tuned 0.25 → 0.30 on the same two years.
- Mean-reversion thresholds were relaxed (three parameters at once) on the same
  two years.
- 8 strategies × dozens of parameters have been screened against **2024–2026 on
  five names** — and 2024–2026 was a historically strong run for exactly these
  megacap tech names. There is no held-out period anywhere in the loop.

To the project's real credit, two habits already push the right way: the
cross-year consistency requirement (a crude out-of-sample proxy), and the
`tqqq_momentum` work, where a 3xATR take-profit that scored **+346% in-sample was
rejected** as "a jagged parameter spike (3.5xATR fell to +144%)" — that is
genuine parameter-stability reasoning, and it is exactly the right instinct. The
proposal is to make that instinct systematic rather than occasional.

**Implementation sketch.**
- **Hold out data.** `cache/market_data.db` has 2016–2026 and `backtest_history.py`
  already runs the full range. Freeze a block (e.g. 2016–2021) as untouchable
  out-of-sample, tune only on the rest, and report the held-out result *once*.
- **Log every configuration tested**, not just the winners. `dashboard/db.py`
  already has a `research_experiments` table and `db_mod.log_experiment(...)`.
  The number of trials is the input the Deflated Sharpe Ratio needs — and it is
  currently unrecorded, so selection bias cannot even be estimated today.
- **Report parameter-stability curves, not point results.** The
  3xATR-vs-3.5xATR check that saved the TQQQ strategy should be standard output
  for every tuned parameter: plot performance across the neighborhood and prefer
  plateaus over spikes.
- **Add walk-forward** to `backtest_history.py`: re-fit on a rolling window,
  evaluate on the following unseen window, and concatenate.

**Effort:** moderate. **Risk:** none to capital — and it is the only item here
that makes every *other* item's results trustworthy.

**Uncomfortable but honest implication:** applying this properly will probably
show some current headline numbers to be optimistic. That is the point.

---

### 3.8 Fractional Kelly sizing

**The idea.** Size positions by the Kelly criterion, f* = (bp − q)/b, which
maximizes long-run geometric growth — then bet a *fraction* of it.

**Evidence.** Full Kelly is optimal only in the mathematical limit where win rate
and payoff are known exactly and are stationary; both are estimates in live
trading, and betting full Kelly on an overestimated edge is a standard way to
blow up. Half-Kelly captures roughly 75% of the growth for half the position
size, with drawdown shrinking roughly in proportion — which is why most
practitioners use 25–50% of full Kelly. Browne and Whitt (1996) found that
overestimating edge while betting Kelly leads to catastrophic drawdown outcomes
([QuantStrategy.io](https://quantstrategy.io/blog/applying-the-kelly-criterion-to-trading-maximizing-growth/)).

**Fit here — listed on the roadmap (`AGENTS.md`, "Adaptive position sizing (Kelly
criterion)"), but I would not do it next.** Kelly requires a reliable estimate
of win rate and payoff ratio. This bot has 39 live trades, all from a bull
market, and backtested win rates that were themselves produced by the tuning loop
described in 3.7. Kelly sizing computed from an overfit win rate is precisely the
"overestimated edge" failure case, and it is *amplifying* — it increases size
exactly where the estimate is most inflated.

Volatility targeting (3.1) achieves much of the same "size to conditions" benefit
using ATR, which is *measured* rather than *estimated from strategy performance*,
and is therefore far more robust. **Do 3.1 first; revisit Kelly only after 3.7
provides trustworthy win-rate estimates**, and then at no more than half Kelly.

**Effort:** low to implement, high to justify. **Risk:** high if done on current
statistics.

---

## Part 4 — Suggested sequence

Ordered so that each step makes the next one more trustworthy.

| # | Item | Why this order | Effort |
|---|---|---|---|
| 1 | **3.7 Validation methodology** | Nothing else can be evaluated honestly until this exists. Held-out period + trial logging first. | Moderate |
| 2 | **3.3 Index regime filter** | Closes the identified bear-market gap; small, self-contained, mirrors the existing kill-switch pattern. | Low |
| 3 | **3.1 Volatility-targeted sizing** | Largest documented risk-adjusted-return gain; ATR already computed and unused for sizing. | Moderate |
| 4 | **3.2 Correlation-aware caps** | Generalizes `leveraged_headroom()`, which already exists; the 0.66 stress correlation justifies it. | Low |
| 5 | **3.4 Ranked slot allocation** | Removes arbitrary first-come slot filling. Cheap. Full cross-sectional momentum waits for #6. | Low |
| 6 | **3.6 Universe expansion** | Precondition for real diversification and for #7. Do after caps exist so it cannot raise risk. | Low–High |
| 7 | **3.5 Meta-labeling** | Only viable with #1, #6 and a decade of labeled events. | High |
| 8 | **3.8 Fractional Kelly** | Only after #1 yields trustworthy win rates. Half Kelly maximum. | Low/High |

A note on ordering philosophy that matches this repo's existing instincts: items
2–4 are all **risk-management** changes, not signal changes. They are the ones
that make the system survivable, and — unlike new entry signals — they cannot be
overfit into looking good, because they do not touch what the strategies predict.

---

## Part 5 — Things deliberately not recommended

- **Shorting / inverse ETFs in a downturn.** The long-only design is a
  considered constraint and should stay. The right response to a bear market
  here is the regime filter (3.3) — be in cash, not short.
- **More entry signals in the existing family.** The bot has six overlapping
  variants of trend/pullback logic already. A seventh adds testing burden and
  selection bias without adding an edge.
- **Deep learning on price data.** With ~40 live trades and a five-name universe,
  any neural approach would fit noise. Meta-labeling (3.5) is the disciplined
  version of "add ML," and even that is gated behind sample size.
- **Intraday / higher-frequency signals.** The 4h + completed-bar discipline in
  `data_feed.completed_bars` is a real safeguard against lookahead bias. Going
  faster increases cost sensitivity and would require modeling slippage and
  spread, which the backtests currently do not do at all.

---

## Sources

- [An Introduction to Volatility Targeting — QuantPedia](https://quantpedia.com/an-introduction-to-volatility-targeting/)
- [Vol Targeting and Trend Following — Rob Carver](https://qoppac.blogspot.com/2018/07/vol-targeting-and-trend-following.html)
- [Position Sizing in Trend-Following: Volatility Targeting, Volatility Parity, and Pyramiding — Concretum Group](https://concretumgroup.com/position-sizing-in-trend-following-comparing-volatility-targeting-volatility-parity-and-pyramiding/)
- [The Impact of Volatility Targeting — CFA Institute](https://rpc.cfainstitute.org/research/cfa-digest/2019/07/dig-v49-7-2)
- [Time-Series vs. Cross-Sectional Implementation of Momentum, Value and Carry — QuantPedia](https://quantpedia.com/time-series-vs-cross-sectional-implementation-of-momentum-value-and-carry-strat/)
- [Time-series and cross-sectional momentum strategies under alternative implementation strategies — Bird, Gao & Yeung (2017)](https://journals.sagepub.com/doi/10.1177/0312896215619965)
- [I Tested 20 Trend-Based Regime Filters — setup4alpha](https://setup4alpha.substack.com/p/i-tested-20-trend-based-regime-filters)
- [Asset Class Trend-Following — Quantpedia](https://quantpedia.com/strategies/asset-class-trend-following)
- [US Stock Momentum Trading System for Retail Traders — Cracking Markets](https://www.crackingmarkets.com/us-stock-momentum-trading-system-for-retail-traders-deep-research/)
- [The Deflated Sharpe Ratio — Bailey & López de Prado (SSRN)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551)
- [Purged cross-validation](https://en.wikipedia.org/wiki/Purged_cross-validation)
- [Backtest Overfitting in the Machine Learning Era — ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S0950705124011110)
- [Meta-Labeling — Wikipedia](https://en.wikipedia.org/wiki/Meta-Labeling)
- [Does Meta-Labeling Add to Signal Efficacy? — Hudson & Thames](https://hudsonthames.org/does-meta-labeling-add-to-signal-efficacy-triple-barrier-method/)
- [Why Meta-Labeling Is Not a Silver Bullet — QuantConnect](https://www.quantconnect.com/forum/discussion/14706/why-meta-labeling-is-not-a-silver-bullet/)
- [Applying the Kelly Criterion to Trading — QuantStrategy.io](https://quantstrategy.io/blog/applying-the-kelly-criterion-to-trading-maximizing-growth/)
- [How correlation impacts diversification — Saxo](https://www.home.saxo/learn/guides/diversification/how-correlation-impacts-diversification-a-guide-to-smarter-investing)
- [Concentration Risk — Britannica Money](https://www.britannica.com/money/concentration-risk-management)
