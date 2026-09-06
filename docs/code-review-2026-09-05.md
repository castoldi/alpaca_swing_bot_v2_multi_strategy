# Code review and strategy research assessment

**Reviewed:** 2026-09-05  
**Code baseline:** `0063eafe3978cb463154ae3a9c70928f3bced7c1`, version `0.24.1`  
**Scope:** order execution, reconciliation, singleton management, market data, shared indicators, all eight registered strategies, portfolio backtests, and research evaluation. Dashboard and tax integration were inspected selectively; this is not a comprehensive security or tax audit.

## Assessment

Fix execution ownership and protective-order lifecycle first. Repair the simulator and statistical evaluation before using existing performance rankings to select or tune a strategy. The code has useful safeguards, but several current results describe behavior the running bot cannot reproduce.

The strongest foundations are paper-only broker construction, durable entry/exit intent, strategy-specific universes, whole-share cash limits, completed-candle filtering, and isolated database fixtures. **The existing suite passed: 395 tests, one dependency deprecation warning.** The findings below expose gaps outside those tests; passing tests do not establish live/backtest equivalence.

This review changed documentation and release metadata only. It did not change trading behavior, restart services, place broker orders, or rerun market-data backtests that overwrite reports or the trading database. Strategy proposals below are hypotheses, not demonstrated improvements in returns.

### Priorities

P1 means address before relying on the affected execution or research result. P2 means a material correctness or validation gap. Evidence is either a synthetic reproduction, direct source tracing, or source tracing combined with broker documentation; none is a claim that an incident occurred in the actual account.

| ID | Priority | Finding | Evidence |
|---|---|---|---|
| F01 — **FIXED** | P1 | Filled brackets can lead to selling another owner's shares | Fixed in v0.24.3 (`b9dd867`); ownership regression tests |
| F02 — **FIXED** | P1 | Multi-day protection uses DAY orders with no active repair path | Fixed in v0.24.5; Alpaca documentation + protection lifecycle regressions |
| F03 | P1 | Manager is not atomic and does not reliably establish project identity | Source trace |
| F04 | P1 | Bracket backtests record the wrong entry price and time | Synthetic reproduction |
| F05 | P1 | Bracket stop simulation fills through gaps at unavailable prices | Synthetic reproduction |
| F06 | P1 | Earnings avoidance is missing live and wrong historically | Source + synthetic reproduction |
| F07 | P1 | Daily-loss backtest accounting can miss losses and use future closes | Synthetic reproduction + source |
| F08 | P1 | A mathematically invalid t-statistic guard corrupts research evidence | Comparison with SciPy |
| F09 | P2 | Adjusted cache can splice prices from different adjustment vintages | Fake data-source reproduction |
| F10 | P2 | Historical SIP and live IEX inputs do not match | Source + broker documentation |
| F11 | P2 | Holding-period rules disagree across config, backtest, and live | Source + synthetic reproduction |
| F12 | P2 | Terminal partial entries retain the wrong quantity and basis | Fake broker reproduction |
| F13 | P2 | An entry-processing exception can skip all exit reconciliation | Fake pipeline reproduction |
| F14 | P2 | RSI treats an uninterrupted rise as neutral | Synthetic reproduction |
| F15 | P2 | Optimizer tests a different timeframe/universe and ineffective parameters | Source trace |
| F16 | P2 | Advertised strategy guards and parameters do not control the stated rules | Source + synthetic reproduction |

## Detailed findings

### F01 — Reconcile owned bracket fills before looking at the aggregate position

**Status: FIXED** in v0.24.3, commit `b9dd867ceaa823400491465764fc302a4771d2a1`.
Validation: 415 tests passed, including 20 new ownership regression tests.
F03 is the next unresolved finding following the F02 resolution below.

**Resolution update — 2026-09-06, v0.24.3:** corrected bracket reconciliation to
validate stored parent references and reconcile linked child fills before any
new exit. Manual exits confirm all linked protection is inactive and subtract
cumulative fills, including fills during cancellation. Completed owned trades
close locally even when foreign shares remain. Added lifecycle regression tests;
the findings below retain their original reviewed-baseline locations. Other
findings remain separate work; persistent protection (F02) was subsequently
resolved in v0.24.5 as recorded below.

**Locations:** [bot.py](../bot.py), lines 1105–1111, 1140–1151, 1826–1827.

`_reconcile_and_exit` calls `_reconcile_closed` when the broker's entire symbol position is absent. On a shared account, this bot's bracket can finish while another owner still holds that symbol. The original entry continues to pass `_verify_owned`, but that proves historical ownership, not remaining ownership. A later time stop can sell again.

**Reproduction:** this bot bought two shares; its two-share TP already filled; a sibling still owns three shares. The real reconciliation/close functions, driven by a fake broker and temporary database, submitted a new **SELL 2**. The local row remained open and recorded zero progress for the completed TP.

**Impact:** another owner's shares can be sold, and the local P&L/position ledger becomes wrong.

**Correction:** ingest cumulative fills from every trade-linked protective order before any close decision. Compute remaining owned quantity from actual entry fills minus actual exit fills. Broker symbol quantity is only an upper bound. Finalize the local trade when its quantity is exhausted even if the account still holds the symbol.

**Regression:** filled owned TP plus a remaining foreign position closes the local ledger with no new order; repeat for partially filled TP, stop fills, and repeated reconciliation.

### F02 — Protective orders expire sooner than the swing position

**Status: FIXED — 2026-09-06, v0.24.5.** New bracket and stop-only OTO entries
use GTC. Reconciliation checks the trade's exact linked protection, remaining
owned quantity, and time in force, and repairs expired/canceled, DAY, or
incorrectly sized protection. It confirms cancellation and records any racing
fills before placing one GTC OCO, or one standalone GTC stop for SMA exits.
Manual exits also cancel replacement stops, and completed replacement fills
close the owned ledger even when foreign shares remain.

Replacement client IDs are persisted before submission and adopted after a
lost response or broker-ID database write. Unresolved submissions, incomplete
ownership evidence, unconfirmed cancellation, or unavailable shares block
further orders. An unresolved submission requires operator reconciliation if
Alpaca never returns the saved client ID; the bot does not blindly retry it.
If protection levels have already been breached, a durable controlled exit
replaces an invalid protective request. Missing daily signal data does not
prevent protection checks.

**API verification:** checked Alpaca's current [order lifecycle and advanced
order rules](https://docs.alpaca.markets/us/docs/orders-at-alpaca),
[create-order contract](https://docs.alpaca.markets/us/reference/postorder), and
[client order ID guidance](https://docs.alpaca.markets/us/docs/working-with-orders).
Requests use whole-share quantities, GTC, no extended-hours execution, OCO limit
take-profit plus stop-loss fields, and the required sell-stop/base-price gap.
GTC is still subject to Alpaca's 90-day expiration policy; reconciliation handles
terminal expiration. Broker-held protection does not guarantee execution outside
regular hours or at the stop price.

**Validation:** 450 tests passed, including 35 new protection lifecycle cases;
one existing dependency deprecation warning. Tests use real SDK requests and an
isolated SQLite ledger with a simulated broker; no live-order compliance test
was submitted. **Next unresolved finding: F03 (singleton process management).**

The description below records the original reviewed baseline.

**Locations:** [bot.py](../bot.py), lines 258–265 and 293–300; unused helpers at 310–376.

Both `_place_stop_only_entry` and `_place_single_bracket_entry` submit `TimeInForce.DAY`. The strategies can hold across sessions. `_protective_orders_missing` and `_place_protective_oco` exist but have no callers.

Alpaca documents automatic cancellation of unfilled DAY orders after the closing auction, permits DAY or GTC for brackets, and applies the same requirements to OTO orders. Consequently the configured protection can expire while the position remains. This is a source/documentation finding, not an inspection of current account protection. [Alpaca order lifecycle and time in force](https://docs.alpaca.markets/us/docs/orders-at-alpaca)

**Correction:** use a persistent protective-order lifecycle appropriate for multi-session holdings. Verify owned protection and its remaining quantity every reconciliation. Recovery must persist intent and adopt an existing replacement before submitting another.

**Regression:** both entry types specify the intended persistent TIF; simulate a session boundary and missing/expired protection; verify exactly one repair or controlled exit. Persistence does not itself provide extended-hours execution.

### F03 — Singleton checks race, and process matching can affect unrelated projects

**Locations:** [scripts/manage.ps1](../scripts/manage.ps1), `Get-BotProcesses`, `Get-BotHealth`, `Start-Bot`, `Stop-Bot`, `Start-Dashboard`, `Stop-Dashboard`; [runtime.py](../runtime.py), `register`/`unregister`; [keep_alive.py](../keep_alive.py), `main`.

The manager performs a health check followed by a launch without a mutex or exclusive lock. Two simultaneous invocations can both see no instance and both launch. `runtime.register` simply overwrites shared files. `unregister` removes them without confirming that the exiting process still owns them.

Identity checks are also insufficient: `Get-BotProcesses` matches any Python command containing `bot.py` and `--strategy`; it does not require this project's path. PID-based stops accept any live PID. Dashboard stop kills the port owner without verifying the project. A reused PID, another checkout, or an unrelated application on the configured port can be adopted or terminated incorrectly.

The scheduled task already uses `MultipleInstances IgnoreNew`, which is useful, but it does not serialize manual manager calls with watchdog launches. Also, watchdog recovery always supplies `ensemble`/30 minutes, potentially replacing an explicitly selected strategy after a failure.

**Correction:** hold a project/service-specific named mutex through check, adoption, stop, launch, and readiness confirmation; validate executable, absolute project identity, process creation time, and service health. Remove runtime files only if ownership still matches. Persist and recover the chosen strategy/interval.

**Regression:** concurrent starts yield one service; a foreign `bot.py --strategy` is untouched; a stale PID and foreign port owner are rejected; exiting an older process cannot remove a newer owner's state. These destructive/concurrency scenarios were not exercised against running services during review.

### F04 — The next-open slippage check does not change the simulated entry

**Location:** [backtest_portfolio.py](../backtest_portfolio.py), lines 601–635.

The bracket path reads the next bar's open into `fill_price` and uses it to reject excessive drift. It then constructs the candidate with `entry_price=signal.entry_price` and `entry_date=signal.date`. The accepted fill remains the previous close and its bar-start timestamp. Signal-exit strategies instead enter on the next bar.

**Reproduction:** signal close $100; next open $101, within the 1.5% guard. The candidate still records $100 at the signal bar timestamp. At a later $108 target, that reports $8/share instead of $7/share, and can change whole-share affordability and cash reservations. Favorable gaps bias in the opposite direction; the general problem is wrong execution accounting.

The signal bar is only known after it closes, so reserving capital at its opening timestamp also shifts portfolio ordering and risk checks backward in time.

**Correction:** represent signal time, information-availability time, and execution time separately. Use the modeled executable fill for shares, cash, P&L, holding duration, and event ordering. Preserve or recompute SL/TP geometry according to the same explicit convention used live.

**Regression:** positive and negative gaps within the guard must affect entry price and P&L; guard rejection must create no candidate; cash from a later event must not finance an earlier fill.

### F05 — Gap stops and trading sessions are not modeled consistently

**Locations:** [strategies/base.py](../strategies/base.py), lines 305–366; [backtest_portfolio.py](../backtest_portfolio.py), `_signal_exit_candidate`.

The bracket engine returns the stop price whenever a later bar's low crosses it. It never checks whether that bar opened below the stop. By contrast, the signal-exit engine already has an opening-gap rule.

**Reproduction:** $100 entry, $90 stop, next bar open $80/high $85/low $75. `simulate_exit` returns **$90**, a price above that entire bar. The scale-out research path has the same issue.

A stop becomes a market order and does not guarantee its trigger price. Brackets are not eligible for extended-hours execution. [Alpaca stop and bracket rules](https://docs.alpaca.markets/us/docs/orders-at-alpaca)

The simulator also processes every nonzero-volume 4h bucket without an exchange-session execution calendar. A premarket/after-hours high or low can trigger a simulated bracket that cannot execute then. A 4h bucket spanning a session boundary cannot resolve this from OHLC alone.

**Correction:** apply gap-aware fills and a session-aware event clock, using finer execution bars when needed. Retain a documented conservative rule for ambiguous bars touching both SL and TP. Add spread/slippage/fees separately; the current entry drift filter is not a transaction-cost model.

**Regression:** opening gaps, both-barrier bars, overnight barrier touches, holidays, early closes, and gaps between signal and the next eligible session.

### F06 — The earnings filter does not protect live entries

**Locations:** [bot.py](../bot.py), lines 668–683; [strategies/base.py](../strategies/base.py), lines 44–78; [strategies/trend_pullback.py](../strategies/trend_pullback.py), lines 28–29.

The live entry path calls `add_indicators` but never `add_earnings_filter`. Trend Pullback checks earnings only if `near_earnings` exists, so live entries pass without this filter.

The helper itself skips every earnings date beyond `df_end`, precisely the upcoming events a live filter needs. It also marks `avoid_days` rows instead of trading days and excludes the final bar before the event through `start:pos`. On a 4h frame, three rows are not three sessions. The process-wide earnings cache has no refresh and permanently caches failures as empty lists; the provider's current event history is not a point-in-time historical calendar.

**Reproduction:** a frame ending today with a supplied earnings event tomorrow produces zero flagged bars.

**Correction:** integrate one calendar-aware event policy in both live and backtest paths. Retain upcoming events, distinguish before/after-market announcements, define trading-session boundaries, and refresh event data. Historical research needs the event schedule known at the decision date and an explicit missing-data policy.

**Regression:** upcoming earnings, same-session events, Friday-to-Monday windows, historical trailing bars, failed refresh, and successful later refresh. Re-evaluate previous claims of earnings-filter P&L improvement after this correction.

### F07 — The backtest daily-loss guard can forget today's losses

**Location:** [backtest_portfolio.py](../backtest_portfolio.py), lines 110–166 and 270–299.

`realize_before(entry_date)` runs before a new day's baseline is captured. If a position exited earlier that day before the first candidate, the baseline uses cash that already contains that realized loss. The simulated drop can vanish.

**Reproduction:** initial equity $1,000; five shares bought at $100; all sell next session at $90 before that day's first new candidate. The account has lost 5%, above the 3% guard. The simulator accepted the new candidate and reported zero blocked entries; ending equity was $950.

There is a second timing issue: `_price_asof` includes the close at the entry timestamp. Bar timestamps denote starts, so for a daily next-open entry it can use that day's final close to assess a loss at the open. The analogous problem applies to 4h bars.

**Correction:** establish prior-session closing equity before processing the next session's fills, independently of candidate arrivals. Mark positions only with prices observable at each execution event. Test daily and intraday clocks separately.

**Regression:** loss before first candidate, no candidates for several sessions, overnight gaps, recovery later in a session, and future-close changes that must not alter an earlier entry decision. The current live guard re-enables entries after recovery, and existing backtest tests preserve that behavior. Keep this an explicit policy rather than inferring it from comments saying “for the rest of the day.”

### F08 — A t-statistic is not bounded by the square root of sample size

**Location:** [research/significance.py](../research/significance.py), lines 127–140.

The code returns `(0.0, 1.0)` if `abs(t) > sqrt(n)` and describes this as numerical noise. That bound is false for a one-sample t-statistic. A finite sample with positive mean and sufficiently small, nonzero variance can legitimately exceed it.

**Reproduction:** returns `[0.01, 0.02, 0.03, 0.04]` produce `(0.0, 1.0)` here. `scipy.stats.ttest_1samp(..., popmean=0, alternative="greater")` returns **t = 3.8729833462, p = 0.0152331458**. This is a formula check, not a claim that four trades establish an investable edge. [SciPy t-test definition](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.ttest_1samp.html)

**Impact:** observed statistics, adjusted p-values, and bootstrap maxima can all be distorted. Zeroing bootstrap tail observations can also lower the search hurdle, so the guard cannot be described as uniformly conservative.

**Correction:** remove the false bound. Handle genuinely degenerate samples and minimum sample/block counts explicitly; compare ordinary finite samples against SciPy. Then rerun saved search panels, including losing variants.

**Regression:** varied samples above and below `sqrt(n)`, zero variance, near-zero variance, negative means, and bootstrap tail behavior. No previous significance verdict should be assumed unchanged.

### F09 — Incremental adjusted-price caching can invent a price discontinuity

**Location:** [market_cache.py](../market_cache.py), lines 243–296; [data_feed.py](../data_feed.py), `_feed_options`.

The cache stores `adjustment="all"` but downloads only missing edges of an already covered interval. A later split/dividend adjustment can revise historical prices that the cache never fetches again. Appending new data can join different adjustment vintages.

**Reproduction:** a fake source initially returns old adjusted closes of $100. After an adjustment revision it returns $50 for the same old history and new bars. Extending the cached interval produces **[100, 100, 50]**, rather than a consistently revised series. This was reproduced in a temporary cache, not asserted about current stored data.

**Correction:** track corporate-action/adjustment versions and invalidate affected history, or store raw prices and a versioned corporate-action ledger. Record the data snapshot used for every research run.

**Regression:** cache before a split, append afterward, compare with a fresh full-range fetch. Include historical provider corrections, dividends, and repeated refreshes.

### F10 — Historical and live strategies consume different feeds

**Locations:** [backtest_2025.py](../backtest_2025.py), `download_history`; [data_feed.py](../data_feed.py), `fetch_bars`, `fetch_recent`, `fetch_snapshots`; [bot.py](../bot.py), `fetch_bars`.

Annual backtests request SIP; live recent bars default to IEX and snapshots explicitly request IEX. SIP consolidates exchanges; IEX is one exchange, so volume and OHLC differ. This particularly affects breakout volume thresholds, highs, and close-based crosses. [Alpaca feed comparison](https://docs.alpaca.markets/us/docs/market-data-faq)

**Correction:** make signal feed/session policy an explicit shared configuration and preserve it in report metadata. If live remains IEX, validate on IEX; if moving to SIP, verify account access and latency before changing it. Separately measure reference-price freshness and bid/ask execution costs.

**Regression:** assert the same configured feed reaches both data paths; compare signal disagreements on aligned IEX/SIP snapshots before treating an old result as a live forecast.

### F11 — “Days” are bars in backtests and rounded calendar days live

**Locations:** [config.py](../config.py), holding-period fields; [strategies/base.py](../strategies/base.py), lines 305–366; [bot.py](../bot.py), lines 1010–1014 and 1140–1146.

`simulate_exit` compares `bars_held` with `max_holding_days`. Live divides that parameter by two, rounds it, then compares calendar dates. The assumption of two bars per day is not an exchange calendar and fails across weekends, holidays, and extended-hours buckets.

**Reproduction:** configuring a limit of two on a 4h frame exits a breakeven-or-better position after eight hours. Config/docs describe these parameters as days. A Friday entry can also reach a live calendar threshold over a weekend without the equivalent number of tradable bars.

**Correction:** choose and document one unit—elapsed time, trading sessions, or completed bars—and implement it identically. Count from the actual fill. The existing breakeven condition means this is an eligible time-exit threshold, not a maximum lifetime: losing positions can remain until a stop or signal exit.

**Regression:** Friday entries, holidays, short sessions, multiple 4h buckets, exact threshold boundaries, and underwater positions.

### F12 — Canceled partial entries retain requested shares

**Locations:** [bot.py](../bot.py), lines 426–439 and 1441–1446.

`_backfill_entry_fill` accepts only `filled`/`closed`. A parent canceled or expired after a partial fill is neither backfilled nor treated as a never-filled entry.

**Reproduction:** request five shares; parent cancels after filling two at $101. Local ownership remains five shares with no filled-price field. Recording the actual complete disposal of two shares at $110 fails to finalize the trade because it still expects five.

**Correction:** separate requested quantity from cumulative filled quantity. For terminal entry states with nonzero fills, persist actual average cost and owned shares; manage protection for that residual. Always limit subsequent exits to current owned inventory.

**Regression:** canceled and expired partial entries followed by disposal reconcile correct basis, quantity, $18 gross P&L, and closed status. Repeated updates must be idempotent.

### F13 — Entry exceptions can bypass risk management

**Location:** [bot.py](../bot.py), lines 668–678 and 977–990.

The ticker scan and exit phase share an outer `try`. An unexpected exception during data preparation, indicator computation, or entry processing jumps over normal reconciliation for all holdings.

**Reproduction:** injecting an exception from the entry `fetch_bars` call makes `run_once` return 1 with zero calls to reconciliation. Normal `data_feed.fetch_bars` catches many provider errors and returns an empty frame, so this finding concerns exceptions that escape that wrapper or arise downstream; it does not mean every ordinary network failure skips exits.

**Correction:** isolate ticker entry failures and run broker reconciliation independently of successful entry scanning. Exits requiring a fresh signal may defer if that signal is unavailable, while broker-fill accounting and time-stop handling should still execute.

**Regression:** failure on one ticker still reconciles pending exits and available time stops for other holdings, without generating entries from invalid data.

### F14 — RSI returns 50 for an uninterrupted gain sequence

**Location:** [strategies/base.py](../strategies/base.py), lines 90–99.

Zero average loss is converted to NaN, then every NaN is filled with 50. With positive average gain and no losses, RSI should reach 100 under the formula's limiting case. This also merges unavailable warmup values and neutral values.

**Reproduction:** an 80-observation strictly rising close series returns 50.0 on the last observation. It can suppress RSI-above-50/rising tests and alter ensemble member votes.

**Correction:** handle positive-gain/zero-loss, zero-gain/positive-loss, and completely flat series separately. Make warmup validity explicit.

**Regression:** rising, falling, flat, and mixed series; compare standard nondegenerate calculations and preserve strategy warmup requirements.

### F15 — Optimizer results are not production-strategy results

**Locations:** [research/optimizer.py](../research/optimizer.py), lines 36–85 and 166–203; [backtest_history.py](../backtest_history.py), `run_independent_annual_portfolios`.

The optimizer downloads daily yfinance data for every strategy, although seven strategies run on 4h bars. It iterates `TICKERS` instead of `strategy_universe`, so `tqqq_momentum` is evaluated on the five ordinary stocks rather than TQQQ. It does not pass `price_frames`, disabling the simulated daily-loss guard. The historical multi-year helper also omits frames, unlike the annual runner.

The random search always mutates the same four Trend Pullback-oriented fields. Breakout, MACD, Mean Reversion, and TQQQ use other stop/TP fields; many requested sweeps therefore repeat identical behavior. It constructs a new `StrategyParams` instead of replacing fields on the supplied baseline.

**Correction:** reuse one timeframe/feed/universe/portfolio runner, pass the full risk configuration, and define each strategy's effective parameter space. Cache one immutable dataset per experiment. Distinguish genuinely repeated trials from distinct evaluated variants and log both.

**Regression:** spy on source requests to verify TQQQ-only/4h and SMA-only/daily cases; compare optimizer baseline trades with the annual runner; verify each searched parameter affects its intended rule on a controlled fixture.

### F16 — Several strategy descriptions do not match their predicates

**Locations:** [strategies/breakout.py](../strategies/breakout.py), lines 29–44; [strategies/mean_reversion.py](../strategies/mean_reversion.py), lines 25–39; [strategies/ensemble.py](../strategies/ensemble.py), lines 15–22 and 52–56; [strategies/regime_adaptive.py](../strategies/regime_adaptive.py), risk branches; [config.py](../config.py), `regime_risk_off_mult`.

| Rule | Actual behavior | Implication |
|---|---|---|
| Breakout abnormal-range rejection | Compares two heavily overlapping averages, both containing the current bar | A final bar with $101 high-low range after $2-range bars still produces a signal in a controlled fixture |
| Breakout “price breaks” | Tests current high above the prior high, not closing acceptance above it | An intrabar rejection can qualify; clarify whether this is intentional |
| Mean Reversion near lower Bollinger band | Only checks that `bb_lower` is non-NaN; never compares price to it | Changing the band multiplier does not enforce the advertised proximity condition |
| Ensemble requires multiple members | Regime weight 0.35 alone exceeds threshold 0.30 | It is effectively regime entries plus selected other combinations, not a strict consensus gate |
| Defensive regime stop multiplier | `regime_risk_off_mult=0.7` is unused; risk-off uses the ordinary stop percentage | Tuning that parameter has no effect |

**Correction:** explicitly decide whether each description or predicate is authoritative. Correct no-op parameters and failed guards first. Treat added confirmation requirements as strategy changes requiring separate evaluation.

**Regression:** a wide current breakout bar is compared with prior completed ranges; a price far from the lower band fails if band proximity is intended; regime-only votes have an explicit expected result; varying each advertised parameter changes the controlled decision or documented risk level.

## Additional limitations affecting performance interpretation

1. **Drawdown is primarily realized-only.** Annual `compute_max_drawdown` and the portfolio equity curve omit unrealized excursions between exits. A position that falls deeply and recovers can show little realized drawdown. The optimizer has an additional definite bug: it omits initial equity from the peak series; a single $100 loss on a $1,000 account reports **0%** maximum drawdown instead of 10%. Use daily or finer mark-to-market equity with an initial observation.
2. **Sizing parity is incomplete.** `run_annual_portfolio` sizes from initial equity plus realized P&L and tracks leveraged exposure at cost; live sizing reads account equity and position market value. With unrealized moves they diverge. The annual reset convention is intentional, but its mean yearly ROI is not a continuous-account CAGR.
3. **Fully adjusted prices change historical whole-share affordability.** Even a consistent split-adjusted series can make a stock appear affordable in a $200 historical allocation when its then-traded nominal share price was much higher. Use corporate-action-aware raw execution prices/quantities, or clearly label the result as an adjusted-price approximation. Dividend-adjusted execution prices also need an explicit cash-dividend accounting convention.
4. **Selection bias remains after multiple-testing correction.** Today's five-name universe is concentrated in technology/growth; replaying it over history does not test how a universe would have been chosen then. Earlier research already used 2025/2026 and much of 2016–2026. Those periods cannot now become untouched validation data by relabeling them. Per-trade returns overlap across correlated names; a month bootstrap by entry date does not fully reproduce account-level daily exposure or cross-month holdings.
5. **Equal-time candidate ordering is a policy.** Backtests sort by timestamp/ticker, whereas live scans configured ticker order. With scarce cash or slots, different names win. Use a shared deterministic ranking rule and evaluate its selection effect.
6. **Dashboard access assumes a trusted network.** `dashboard/server.py` enables wildcard CORS and unauthenticated account/trade endpoints; the manager binds all interfaces. This exposes financial data to clients that can reach the service. Network exposure was not tested. Document that trust boundary or add access control before widening reachability.

## Strategy improvements to evaluate after correctness repairs

### Research order

First generate corrected baselines. Previously rejected broad market gates, volatility targeting, and stop-width sweeps are recorded in [bear-market-defence.md](bear-market-defence.md). Do not present them as new successful ideas. They may merit re-evaluation only after identifying how the corrected engine changes the original experiment, with the earlier variants included in the research history.

The following is a proposed shortlist, not a request to enable anything live:

| Strategy | Proposed experiment | Why it is relevant | Evidence required to keep it |
|---|---|---|---|
| Trend Pullback | Correct earnings policy first; then test pullback depth normalized by ATR rather than only a recent RSI minimum | A recently low RSI can admit a trade after price has already recovered far from the pullback | Paired net-return improvement across validation folds; earnings/session attribution; stable results around the chosen depth |
| Breakout | Compare close-confirmed breakouts with current high-only breakouts; use current range versus prior ranges and session-matched relative volume | Avoid buying a failed intrabar breakout; current volume mixes dissimilar time buckets | Lower false-breakout loss after costs without relying on one ticker/year; signal-count and missed-winner accounting |
| Mean Reversion | Test actual lower-band distance and an exit toward a predeclared mean | The current band is not an entry constraint; mean reversion should be assessed against its own recovery target | Improvement over the corrected current rule after turnover costs; report holding time and tail losses |
| MACD Momentum | Test histogram slope/ATR as a continuous entry score, keeping the crossover baseline | A binary zero cross discards strength information and may trigger on tiny moves | Stable rank buckets and incremental net value; no narrow parameter peak |
| Ensemble | Ablate regime-only versus current weights, equal weights, and a two-member requirement | Regime alone already passes and members share indicators, so vote count is not independence | Incremental value over regime alone using paired portfolio returns; signal-overlap and concentration analysis |
| Regime Adaptive | Test a minimum dwell period or hysteresis around EMA regime changes; make risk-off exposure a separate decision | Frequent transitions can create turnover, while current risk-off behavior still buys falling names | Fewer transition losses and improved risk-adjusted results across several regimes; compare lost rebound gains |
| SMA 50 Cross | Test a small predeclared cross buffer or confirmation rule | Daily crosses can whipsaw near the SMA, but extra confirmation may enter late | Stability across nearby buffers and longer trends; explicit delayed-entry cost; keep next-session execution |
| TQQQ Momentum | Validate session/fill/stop parity and measure combined exposure with technology holdings before adding indicators | Existing strategy notes already rejected additional EMA/MACD entry gates; leverage amplifies shared exposure | Improved account-level drawdown/tail behavior and retained net return under cost/gap stress; no increase in the current exposure cap without separate evidence |

TQQQ targets three times the **daily** Nasdaq-100 return; multi-day results can differ materially because returns compound. A percentage stop is not a guaranteed loss cap. Use actual ETF prices for the replay, not three times a multi-day index return. [ProShares TQQQ objective](https://www.proshares.com/our-etfs/leveraged-and-inverse/tqqq)

### Portfolio improvements with a concrete objective

**Control loss exposure, not just position count.** At a 20% allocation and a 9% stop, one position risks about 1.8% of equity at the trigger price before gaps/costs; five similar positions represent about 9%. The 3% daily entry halt does not liquidate that risk. Test a capped risk budget such as:

```text
shares = floor(min(
    equity * position_fraction / executable_price,
    available_cash / executable_price,
    equity * per_trade_risk_budget / (executable_price - stop_price)
))
```

Only apply the formula for a valid positive stop distance; retain whole shares, five-position maximum, cash constraints, and the leveraged cap. Evaluate drawdown and capital utilization alongside return. This is a different objective from mechanically maximizing annual P&L. Defer Kelly sizing until net-return estimation and dependence are reliable.

**Measure concentration before adding a correlation gate.** Record aggregate technology/semiconductor and Nasdaq-related exposures, including TQQQ. Compare the baseline with a small, predeclared cluster cap or risk-based admission rule. Record rejected opportunities and lost gains. Do not infer diversification from the number of strategy classes.

**Separate exit efficiency from win rate.** The breakeven-gated time exit can realize small winners while retaining losers. Report maximum adverse/favorable excursion, time underwater, holding-time percentiles, and return per capital-day. Test a finite loss-taking time exit or trend-invalidation exit as a standalone variant. More wins alone are not evidence of improvement.

## Validation and implementation sequence

1. **Execution correctness:** F01–F03 and F12–F13. Use fake broker lifecycles and isolated process tests. Preserve paper-only mode and prove order idempotency, inventory ownership, and protection after restart/session boundaries.
2. **Research correctness:** F04–F11 and F14–F16. Centralize execution time, session/feed configuration, historical price basis, risk settings, and strategy universe across annual, historical, and optimizer paths. Recompute baselines with immutable data/run metadata.
3. **Risk reporting:** mark-to-market equity, net returns, actual maximum drawdown, exposure, turnover, gap losses, and capital utilization. Include cash and a same-universe buy-and-hold benchmark with comparable capital and data conventions.
4. **Controlled experiments:** register a small hypothesis set, parameter ranges, costs, and acceptance criteria before running it. For already-used history, use rolling train/validation folds with boundaries that prevent overlapping trade labels from leaking information. Reserve genuinely unseen future paper results for final confirmation. Keep losing variants in the trial ledger.
5. **Accept only supported changes:** compare paired baseline/candidate account returns; report uncertainty, effect size, worst folds, and dependence-aware bootstrap results after fixing F08. Both 2025 and 2026 improving remains a project requirement, but those reused years are not independent proof. Multiplicity correction addresses search selection, not erroneous fills, missing costs, or lookahead. [Harvey and Liu, False (and Missed) Discoveries in Financial Economics](https://arxiv.org/abs/2006.04269)

For future code changes, update changelog/version and rerun targeted tests plus the suite. Restart affected services only through `scripts/manage.ps1` and verify identity and health. This document itself requires no service restart.

## Verification record

Executed from the repository root with `.venv\Scripts\python.exe`:

```text
python -m pytest
395 passed, 1 warning in 25.87s
Warning: websockets.legacy deprecation, surfaced through an Alpaca import.
```

Additional offline probes used synthetic pandas frames, patched event data, fake broker responses, and temporary SQLite databases. No credentials or account data are included here.

| Probe | Observed result at reviewed baseline |
|---|---|
| Filled own TP, foreign shares remain | An additional two-share sell was submitted |
| Canceled five-share request with two actual fills | Local shares remained five; a two-share disposal could not finalize |
| Exception during entry data fetch | Run returned 1; reconciliation called zero times |
| $100 signal / $101 next-open entry | Candidate price remained $100 at signal timestamp |
| $90 stop / next bar entirely below $85 | Simulated stop fill was $90 |
| Upcoming earnings supplied for tomorrow | Zero bars marked near earnings |
| Two-unit holding limit on 4h synthetic bars | Time stop after eight hours |
| Realized 5% daily loss before first new candidate | Two positions accepted in total; zero kill-switch blocks |
| Strictly rising 80-point price sequence | RSI ended at 50.0 |
| $101 current range following $2 ranges | Breakout signal accepted |
| `[1%, 2%, 3%, 4%]` t-test | Local t=0/p=1; SciPy t=3.872983/p=0.015233 |
| Single $100 loss, optimizer starting equity $1,000 | Reported max drawdown 0% |
| Adjusted-price revision followed by cache extension | Cached closes `[100, 100, 50]`; older bars never refetched |

The manager findings were established by source inspection; no duplicate process was deliberately started. Protective-order expiry was checked against official broker documentation; current account orders were not inspected. Full strategy profitability and any improvement magnitude remain unmeasured by this review.

### Minimal independent reproductions

Run this Python snippet from the repository root. It uses pure calculation functions and a patched earnings provider:

```python
from unittest.mock import patch
import pandas as pd
from scipy.stats import ttest_1samp
from strategies.base import EntrySignal, add_earnings_filter, rsi, simulate_exit
from research.significance import t_statistic

print("rising RSI:", rsi(pd.Series(range(1, 81), dtype=float)).iloc[-1])

frame = pd.DataFrame(
    {"open": [100, 80], "high": [101, 85],
     "low": [99, 75], "close": [100, 82]},
    index=pd.date_range("2026-09-01", periods=2),
)
signal = EntrySignal(frame.index[0], 100, 90, 108, 2, 55)
print("gap exit:", simulate_exit(frame, 0, signal)[1:3])

with patch("strategies.base._get_earnings_dates",
           return_value=[pd.Timestamp("2026-09-03")]):
    print("earnings flags:", add_earnings_filter(frame, "TEST").near_earnings.sum())

returns = [.01, .02, .03, .04]
print("local t-test:", t_statistic(returns))
print("SciPy t-test:", ttest_1samp(returns, 0, alternative="greater"))
```
