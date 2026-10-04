# Research Experiments Log

## Experiment 6: Correlated-group exposure caps (2026-09-22)

**Goal:** R04 of [the 2026-09-22 review](../docs/code-review-2026-09-22.md). The
universe is five mega-cap tech/semiconductor names and the portfolio has five
20% slots, so it regularly goes 100% into one factor. On 2026-09-21 four names
were entered within 14 s on the same bar. The question: does capping a
correlated group cut drawdown enough to be worth the P&L it gives up?

**Setup:** `research/group_cap_experiment.py`, five variants declared before
running (trials = 5): **A** no cap (live), **B** NVDA+AMD+ARM ≤ 40% of equity,
**C** the same semis ≤ 20%, **D** all five names ≤ 60%, **E** all five ≤ 40%.
Live Ensemble on the corrected engine (IEX 4h signals, one-minute execution,
daily-loss guard, tax guard). Candidates are collected once per year and every
variant runs the same annual portfolio runner with only `exposure_groups`
changed. Equity starts at **$100,000** (about the live account) rather than
$1,000, because whole-share sizing at $1,000 distorts a dollar cap.

**Decision rule (fixed before running):** adopt a cap only if, against A, its
max drawdown is lower in at least 4 of 5 years, it keeps at least 75% of A's
total P&L, and its return-to-drawdown ratio (total P&L / mean max drawdown)
beats A's.

| Variant | 2022 | 2023 | 2024 | 2025 | 2026 YTD | Total | Mean max DD | P&L vs A |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A none (live) | −$38,314 (43.8%) | +$94,634 (10.7%) | +$66,335 (13.0%) | +$34,946 (31.1%) | +$40,740 (13.4%) | **+$198,340** | 22.4% | 100% |
| B semis ≤ 40% | −$38,202 (43.7%) | +$85,736 (10.7%) | +$46,917 (9.3%) | +$14,265 (27.1%) | +$44,382 (9.2%) | +$153,099 | 20.0% | 77% |
| C semis ≤ 20% | −$33,341 (38.2%) | +$55,307 (9.3%) | +$24,850 (5.4%) | +$9,750 (20.9%) | +$23,502 (7.1%) | +$80,068 | 16.2% | 40% |
| D all ≤ 60% | −$37,358 (42.0%) | +$64,196 (10.5%) | +$37,537 (10.2%) | +$11,388 (24.7%) | +$39,668 (9.1%) | +$115,430 | 19.3% | 58% |
| E all ≤ 40% | −$28,790 (32.3%) | +$42,781 (8.0%) | +$11,461 (10.1%) | +$14,445 (17.2%) | +$19,527 (10.7%) | +$59,424 | 15.7% | 30% |

Each cell is P&L (max drawdown).

**Findings:**
- Every cap lowers drawdown: B in 4 of 5 years, C–E in all five. None lowers it
  enough to pay for itself. Return-to-drawdown is best for **A**; B comes closest
  (0.86× A's ratio at 77% of A's P&L) and fails only that test.
- The caps cut P&L roughly in proportion to the exposure they remove, while
  drawdown falls less than proportionally. The positions are highly correlated,
  so a cap acts like a smaller position size, not like diversification. Real
  diversification needs different assets, not a limit on the same ones.
- 2022 barely moves under B/D (its 43.8% drawdown stays at 42–44%). The bear-market loss
  is not a same-bar concentration problem; the
  [bear-market playbook](../docs/bear-market-playbook.md) covers it.

**Verdict: REJECTED — keep `exposure_groups = ()`.** The mechanism stays in the
code (live and backtest), off by default, ready for a diversified universe.
Search-corrected evidence for A: t = 3.89 vs hurdle 3.00 over 5 variants. Caveats
as in Experiment 5 (overlapping trades; 2022–2024 not out-of-sample). Logged via
`log_experiment` with evidence.

## Experiment 5: Ensemble vote-gate ablation on the corrected engine (2026-09-21)

**Goal:** V05 of [the verification review](../docs/code-review-2026-09-21-verification.md).
F16 had deployed a 2-vote Ensemble rule without evaluation; v0.25.0 restored the
1-vote rule pending this test.

**Setup:** `research/ensemble_vote_ablation.py`, four variants declared before
running (trials = 4): **A** live (score ≥0.30, ≥1 vote), **B** ≥2 votes,
**C** Regime vote only, **D** equal 0.20 weights. Corrected engine (v0.25.1): IEX
4h signals, one-minute regular-session execution, daily-loss guard, 20% sizing,
$1,000 annual reset, earnings filter with the labelled historical import. One
frozen dataset per year (fingerprints in `reports/ensemble_vote_ablation.json`).

| Variant | 2022 | 2023 | 2024 | 2025 | 2026 YTD | Total | Trades | Worst DD | BHY p |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A live (1 vote) | −$232.67 | +$779.69 | +$362.69 | +$219.42 | +$282.38 | **+$1,411.51** | 636 | 31.8% | 0.0001 |
| B two votes | −$36.41 | +$475.38 | +$188.10 | +$127.62 | +$89.97 | +$844.66 | 383 | 12.8% | 0.0008 |
| C regime only | −$232.67 | +$779.69 | +$362.69 | +$219.42 | +$282.38 | +$1,411.51 | 636 | 31.8% | 0.0001 |
| D equal weights | −$44.17 | +$448.65 | +$145.39 | +$121.79 | +$89.97 | +$761.63 | 392 | 12.8% | 0.0020 |

**Search-corrected verdict for the winner (A):** t = 4.13 vs month-block
bootstrap hurdle 2.48 over 4 variants. It clears, and the haircut Sharpe is 0.15.
No challenger beat A in both 2025 and 2026.

**Findings:**
- **C = A exactly.** Across five years no Ensemble entry qualified without the
  Regime vote. The live Ensemble is therefore Regime's entry signal with the
  Ensemble's 9% stop / 2.5×ATR target / 6-session time stop; the other four
  members change no entries. This confirms the original review's suspicion.
- **B (the F16 rule) halves return but cuts the 2022 loss from −23% to −4% and the
  worst drawdown from 31.8% to 12.8%.** That is a drawdown/return trade-off, not
  an improvement under the project rule (P&L in both 2025 and 2026). Evaluating
  it as a risk-control variant would be a new, separately counted trial with a
  predeclared drawdown objective.

**Verdict: REJECTED challengers; keep A (`ensemble_min_votes = 1`).** Caveats:
trades overlap across correlated tickers, so per-trade t-statistics are
optimistic. 2022–2024 were reused from earlier research and are not
out-of-sample. Earnings decisions before 2026-09-14 rest on the import's
14-day-ahead approximation. Logged via `log_experiment` with evidence.

## Experiment 4: Daily SMA 50 Price Cross (2026-07-18)

**Goal:** Add the simplest possible trend strategy: buy a completed daily close crossing above SMA(50), then sell the opposite cross.

**Option test:** Adjusted daily bars for NVDA, AMZN, META, AMD, and ARM from 2024-01-01 through 2026-07-17; next-open execution; $200/trade; 5 bps cost per side.

| Variant | Trades | P&L | Win rate | Realized max drawdown |
|---------|-------:|----:|---------:|----------------------:|
| Long-only + 10% emergency stop | 108 | **+$1,139.05** | 33.3% | **$176.87** |
| Pure long-only | 108 | +$1,128.73 | 33.3% | $194.32 |
| Long/short reversal | 214 | +$764.54 | 27.6% | $364.32 |
| Existing TP/time-stop overlay | 369 | +$363.96 | 58.8% | $213.92 |

The stop-protected long-only variant had the highest return and lowest drawdown. Long/short reversal was rejected because it underperformed, doubled realized drawdown, added margin/borrow constraints, and introduced unbounded upside risk. Fixed TP and time exits were rejected because they cut trends early.

**Production-engine validation:** Alpaca adjusted daily bars, next-session entry/exit, 10% stop, no modeled transaction cost.

| Year | Trades | Win rate | P&L |
|------|-------:|---------:|----:|
| 2024 | 40 | 25.0% | +$8.15 |
| 2025 | 33 | 48.5% | +$262.87 |
| 2026 YTD | 31 | 22.6% | +$392.68 |
| **Total** | **104** | — | **+$663.70** |

**Verdict: KEPT** — Profitable in both primary evaluation years (2025 and 2026), with a deliberately separate daily timeframe. The broker-held OTO stop protects the position while the normal exit remains the requested SMA cross below. These historical results do not imply future profitability.

Sources used for operational risk decisions: [Alpaca OTO orders](https://docs.alpaca.markets/docs/orders-at-alpaca) and the [SEC short-sale risk bulletin](https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins-51).

## Experiment 3: Revived Mean Reversion Strategy — Relaxed Entry Thresholds (2026-05-28)

**Goal:** Mean Reversion was nearly dead (2 trades in 2026, +$4.06 combined). Relaxed conditions to catch more opportunities in bull markets.

**Change:** Modified `config.py` `StrategyParams`:
- `mr_rsi_oversold`: 48.0 → 50.0 (wider net for "oversold")
- `mr_deviation_pct`: 0.01 → 0.005 (price only needs 0.5% below SMA20, not 1%)
- `mr_bollinger_mult`: 2.0 → 2.2 (recorded at the time as a wider-band entry
  change; F16 later established that the band was diagnostic and never gated entry)

**Results:**

| Metric | Before | After | Improvement |
|--------|--------|-------|-------------|
| 2025 P&L | -$3.94 (11 trades) | **-$3.05 (15 trades)** | Smaller loss, 36% more trades ✅ |
| 2026 P&L | +$8.00 (2 trades) | **+$15.48 (5 trades)** | 94% improvement, 2.5× trades ✅✅ |
| Combined | +$4.06 | **+$12.43** | Tripled ✅✅✅ |
| Ensemble 2026 | +$327.17 | +$329.92 | Slight improvement from MR votes |
| All other strategies | Unchanged | Unchanged | No regressions |

**Verdict at the time: KEPT** — Both years improved. Because the Bollinger field
did not control entry, F16 invalidates attribution of the result to that field;
the RSI and SMA-deviation changes were bundled in the same experiment. Treat the
reported figures as historical, not isolated evidence for a Bollinger rule.

**Goal:** Improve Ensemble strategy P&L by weighting strategies based on actual cross-year performance.

**Change:** Rebalanced `ENSEMBLE_WEIGHTS` in `strategy.py`:
- trend_pullback: 0.30 → 0.20
- breakout: 0.25 → 0.15 (inconsistent across years)
- mean_reversion: 0.10 → 0.05 (barely profitable)
- momentum_macd: 0.20 → 0.25 (consistent performer)
- regime: 0.15 → 0.35 (best cross-year performer: +$333 combined)

**Results:**
| Metric | Before | After | Improvement |
|--------|--------|-------|-------------|
| 2025 P&L | -$28.15 | **+$194.44** | +$222.59 ✅ |
| 2026 P&L | +$113.36 | **+$319.11** | +$205.75 ✅ |
| Combined | +$85.21 | **+$513.55** | +$428.34 ✅✅ |

**Verdict at the time: KEPT** — Both years improved in that run. F16 later added
the advertised two-member requirement and corrected member behavior, so the #1
ranking and recommendation are withdrawn pending corrected baseline regeneration.

## Experiment: Short-only bot (2026-09-26)

**Question:** what if the bot only shorted? `research/short_only_mirror.py` inverts
every price (P' = K/P, high' = K/low) so the unchanged strategy code fires its
signals on the mirror image, then recomputes each trade as a real short
(1 − exit/entry). Same path for both sides: legacy 4h next-open fills, tax guard on,
kill switch off, $1M notional, 20% slots, no costs/borrow, non-compounded.
Mirrored bracket on the real ticker: stop ≈ 9.9% above, target ≈ 4.8% below.
Trials counted: 12 (6 strategies × 2 sides planned).

**Ensemble, SIP 4h, NVDA AMZN META AMD ARM, 2016 – 2026-09-09**

| Year | Long | Short-only |
|---|---:|---:|
| 2016 | +53.7% | −38.5% |
| 2017 | +37.5% | −29.5% |
| 2018 | +15.5% | −14.9% |
| 2019 | +37.1% | −34.3% |
| 2020 | +42.3% | −34.2% |
| 2021 | +34.3% | −30.0% |
| 2022 | −30.6% | **+48.1%** |
| 2023 | +99.6% | −48.6% |
| 2024 | +20.2% | −37.0% |
| 2025 | +10.8% | −21.0% |
| 2026 YTD | +33.9% | −34.4% |
| Per trade | +0.74%, t = +5.95 (2,095) | −0.80%, t = −5.13 (1,809) |

**NVDA only, IBKR 4h, 2005–2026:** short lost 19/22 years (won 2008 +4.1%,
2010 +7.1%, 2022 +5.6%), −1.07%/trade, t = −4.28; long +0.79%/trade, t = +4.10.

Short win rate is 60–75%, but a ~5% target against a ~10% stop on strongly
trending names means the stop-outs cost about 2× the wins. Absolute long numbers
here exceed the official backtests (simplified execution); the long-vs-short
comparison is like-for-like.

**Verdict: REJECTED.** Shorting has no edge on this universe, neither always-on
nor bear-only (docs/bear-market-playbook.md §4). Bear protection: pair `ensemble`
with `tqqq_momentum`. The other five strategies were not run (memory pressure);
rerun with `python research/short_only_mirror.py` if wanted.


## Experiment: Capital size — $1k vs $5k vs $100k (2026-09-26)

**Question:** every tuned number came from $1,000 annual backtests. The planned
live start is ~$5,000. Does capital change the result? (Live-readiness Phase 2.)
`research/capital_size_experiment.py`: ensemble, production engine (minute
execution, earnings filter, kill switch), candidates collected once per year and
replayed at each capital. Drawdown is realized-equity (understates open dips).

| Year | $1k return / DD | $5k return / DD | $100k return / DD | Trades 1k→5k |
|---|---|---|---|---|
| 2022 | −23.3% / 31.8% | **−35.0% / 40.6%** | −38.3% / 43.8% | 135 → 146 |
| 2023 | +78.0% / 8.0% | +90.5% / 10.0% | +94.6% / 10.7% | 156 → 160 |
| 2024 | +36.3% / 10.4% | +59.9% / 12.6% | +66.3% / 13.0% | 161 → 193 |
| 2025 | +21.9% / 16.8% | +31.6% / 28.5% | +34.9% / 31.1% | 131 → 178 |
| 2026 YTD | +27.5% / 2.6% | +34.2% / 11.5% | +39.7% / 13.4% | 54 → 154 |
| 5y compounded | +189% | **+250%** | +277% | |

**Why:** at $1k a slot is $200, so META never traded from 2024 on, and in 2026
AMD/AMZN barely did. At $5k every ticker fits (META is 1 share). $5k behaves
like $100k minus whole-share rounding.

**Verdict:** the $1k baselines understated BOTH return and risk. At the real
$5k size, plan for a bad year like 2022 to cost about a third of the account
(≈ −$1,750) with a ≥40% drawdown along the way. 2024–2026 are in-sample for the
tuning; 2022 is the only bear. Not a parameter change, so no trial correction applies.


## Experiment: Every strategy and three combinations at $5,000 (2026-09-26)

Same harness (`research/capital_size_experiment.py --strategies all --capitals 1000 5000
--combos ...`). Combinations at $5,000 two ways: **shared book** (one account, 5 slots,
all members compete, 20% sizing, leveraged cap — what a multi-strategy process would
do) and **sleeves** ($5,000 / N per member, whole shares, summed). Sleeve drawdown is
approximate (worst sleeve / N). Realized-equity drawdowns throughout.

| Portfolio @ $5k | 2022 | 2023 | 2024 | 2025 | 2026 YTD | 5y comp | Worst yr | Max DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ensemble (live) | −35.0% | +90.5% | +59.9% | +31.6% | +34.2% | **+249.6%** | −35.0% | 40.6% |
| regime | −41.6% | +102.1% | +35.6% | +14.5% | +18.3% | +116.7% | −41.6% | 46.4% |
| sma_50_cross | −16.4% | +50.1% | +2.2% | +22.3% | +42.8% | +123.6% | −16.4% | 16.4% |
| momentum_macd | +6.2% | +27.4% | +4.0% | +7.8% | +11.4% | +68.9% | **+4.0%** | 5.7% |
| trend_pullback | −20.2% | +44.4% | +17.5% | −0.5% | +2.2% | +37.7% | −20.2% | 26.7% |
| breakout | +1.8% | +9.7% | +15.3% | −6.0% | +9.3% | +32.2% | −6.0% | 7.2% |
| tqqq_momentum | −0.5% | +6.4% | −1.2% | +1.4% | −1.4% | +4.5% | −1.4% | 3.9% |
| mean_reversion | −7.4% | +4.0% | −0.0% | −1.0% | +1.4% | −3.3% | −7.4% | 9.8% |
| ensemble + tqqq, shared | −35.0% | +102.2% | +50.3% | +24.8% | +28.7% | +217.1% | −35.0% | 39.7% |
| ensemble + tqqq, sleeves | −15.9% | +45.8% | +27.8% | +11.0% | +16.1% | +101.9% | −15.9% | ~18.5% |
| breakout + tqqq, shared | +1.3% | +16.2% | +13.9% | −4.8% | +7.9% | +37.7% | −4.8% | 9.0% |
| breakout + tqqq, sleeves | +0.8% | +7.5% | +5.9% | −2.8% | +2.3% | +14.1% | −2.8% | ~3.6% |
| **breakout + macd + tqqq, shared** | +8.4% | +37.4% | +20.6% | +3.4% | +16.2% | **+115.9%** | **+3.4%** | 8.0% |
| breakout + macd + tqqq, sleeves | +2.4% | +12.7% | +3.7% | +1.4% | +2.2% | +24.0% | +1.4% | ~2.2% |

**Trial correction** (22 portfolios looked at; per-trade t, optimistic because trades
overlap on correlated tickers):

| | Trades | t | Hurdle | |
|---|---:|---:|---:|---|
| ensemble | 831 | 3.87 | 2.84 | clears |
| momentum_macd | 160 | 4.25 | 2.87 | clears |
| breakout + macd + tqqq, shared | 307 | 4.13 | 2.85 | clears |
| sma_50_cross | 170 | 1.82 | 2.87 | fails |

**Findings**
1. **The recommended `ensemble + tqqq_momentum` does not protect a $5k account.**
   In one shared book, ensemble fills all 5 slots and 2022 is unchanged (−35.0%). As
   sleeves, 2022 halves only because half the money sits idle: the TQQQ sleeve can
   hold at most 20% of $2,500. The earlier −5.5% figure (docs/bear-market-defence.md
   §5) ignored both effects.
2. **`breakout + momentum_macd + tqqq_momentum` in one shared book** is the only
   portfolio that stayed positive every year including 2022 (+8.4%), with an 8%
   drawdown and +116% over 5 years (about half ensemble's +250%). It clears the trial
   correction. It is a survivor chosen from 22 looks on 5 years with one bear, so
   treat "never loses" as a hypothesis for the paper run, not a fact.
3. `momentum_macd` alone also never lost a year, but it's only 160 trades in 5 years.
4. The choice is risk appetite: ~+250% with a −35% year, or ~+116% with a +3% worst year.

Not run: 2020–2021 (IEX 4h history begins 2020-07) and pre-2020 bears. Running two or
three strategies in one process is not built yet (live-readiness 3.2).


## Experiment: Win big, lose small — exit style (2026-09-26)

**Question:** the live exit caps winners (TP 4–12%) and allows a 9% stop, so the
average win (+4%) is half the average loss (−9%). Do trailing exits that let winners
run fix that? `research/exit_style_experiment.py`: the ensemble's own entries, only
the exit re-simulated on 4h bars (stop checked first, gaps fill at the open), then the
real annual portfolio at $5,000. 2016–2021 simple-engine entries, 2022–2026
production entries. 4 variants fixed in advance; R0 is the current exit re-simulated
the same way, so the comparison is like-for-like. (This 4h simulator is more generous
than the minute engine: R0 2022–26 = +384% here vs +250% there.)

| Variant @ $5k | 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 | 2022–26 | 2016–26 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **R0 current** | +54.5 | +30.3 | +9.5 | +36.3 | +46.6 | +35.8 | −29.0 | +106.9 | +66.7 | +33.4 | +48.2 | **+384%** | **+2,800%** |
| W1 trail 3×ATR, no TP | +62.1 | +30.6 | +18.9 | +28.9 | +53.5 | +43.6 | −28.0 | +45.3 | +58.7 | +6.0 | +42.6 | +151% | +1,692% |
| W2 = W1 + 5% stop | +61.2 | +31.5 | +18.3 | +28.7 | +47.9 | +41.6 | −31.6 | +45.1 | +47.9 | +2.5 | +36.1 | +105% | +1,284% |
| W3 = W1 + breakeven at +5% | +60.8 | +28.0 | +13.2 | +29.5 | +51.9 | +43.8 | −29.2 | +44.5 | +55.3 | +6.2 | +41.2 | +138% | +1,471% |

| Variant | Trades | Win % | Avg win | Avg loss | Worst trade | t (hurdle 2.24) |
|---|---:|---:|---:|---:|---:|---:|
| R0 current | 1,901 | 77% | +3.8% | −8.4% | −15.0% | 7.54 |
| W1 | 1,531 | 44% | +6.7% | −3.2% | −11.6% | 5.86 |
| W2 | 1,628 | 41% | +6.8% | −3.0% | −11.0% | 5.38 |
| W3 | 1,569 | 42% | +6.7% | −3.0% | −11.6% | 5.66 |

**Verdict: REJECTED.** The variants did produce "win big, lose small" (avg win +6.7%,
avg loss −3%), but they made far less money: 2022–26 +105–151% vs +384%, and
2023/2025 collapsed. None improved 2022 (−28% to −32%). They helped a little in
2016–2018 and 2020–2021. Why: ensemble's entries buy dips that bounce a few percent and
fade; a fixed target banks the bounce, a trailing stop hands most of it back. Win big,
lose small suits trend-following entries (sma_50_cross, tqqq_momentum), not these.
The payoff asymmetry is the price of a 77% win rate, not a defect in itself.


## Experiment: Holding through earnings (2026-09-27)

**Question:** the worst ensemble trade at $5k was META −27.4% (Feb 2022 earnings gap
through a 9% stop). The live filter only skips entries 3 days before a report. Should
the bot also avoid holding through reports? `research/earnings_exit_experiment.py`,
3 variants fixed in advance, ensemble at $5,000, 2016–2021 simple-engine candidates and
2022–2026 production candidates. Baseline keeps its own engine exits. Report dates from
cache/earnings.db (checked: exactly 4 per ticker per year).

| Variant | 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 | 2022–26 | 2016–26 | Max DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **E0 current** | +53.6 | +36.5 | +15.6 | +36.0 | +41.1 | +32.5 | −35.0 | +90.5 | +59.9 | +31.6 | +34.2 | **+250%** | **+2,054%** | 40.6% |
| E1 skip entry if report in hold window | +42.3 | +39.1 | +9.5 | +25.6 | +34.5 | +24.1 | **−27.2** | +70.1 | +54.6 | +16.2 | +35.8 | +202% | +1,272% | **33.2%** |
| E2 sell at last close before report | +49.9 | +39.7 | +14.5 | +33.1 | +46.6 | +27.6 | −27.3 | +63.9 | +47.7 | +18.1 | +30.7 | +172% | +1,522% | 33.5% |
| E3 both | +40.0 | +39.1 | +9.5 | +24.7 | +34.6 | +23.1 | −27.2 | +66.7 | +54.6 | +14.3 | +31.6 | +182% | +1,142% | 33.2% |

| Variant | Trades | Trades ≤ −10% | Worst trade | Sold pre-report | t (hurdle 2.12) |
|---|---:|---:|---:|---:|---:|
| E0 | 1,859 | 32 | −27.4% | 0 | 6.84 |
| E1 | 1,712 | 26 | −13.7% | 0 | 6.37 |
| E2 | 1,879 | 26 | −13.7% | 84 | 6.48 |
| E3 | 1,714 | 24 | −13.7% | 13 | 6.11 |

**Verdict: works as insurance, not as an improvement — NOT ADOPTED (operator chose max
return, 2026-09-26).** Avoiding reports halves the worst trade (−27.4% → −13.7%), cuts
2022 from −35% to −27% and max drawdown from 40.6% to ~33%. It costs return in most
years: 2022–26 falls from +250% to +172–202%. On average, holding these names through
earnings paid. E1 (skip the entry) is the cheapest version if the tail ever matters more
than return; it is a one-parameter change in how `earnings_avoid_days` is applied.


## Experiment: No stop-loss — take profit + re-entry (2026-10-03)

**Question:** never sell at a loss; keep the take-profit, drop the stop, re-enter on
the next signal. `research/no_stop_experiment.py`, 3 variants fixed in advance × 6
bracket strategies, one continuous $5,000 book (no January reset), mark-to-market.
Full write-ups: [docs/no-stop-2016-2026.md](../docs/no-stop-2016-2026.md) and
[docs/no-stop-bear-stress-test-2004-2015.md](../docs/no-stop-bear-stress-test-2004-2015.md).

| ensemble @ $5k | 2016–2026 | Max DD | 2004–2015 (IBKR, NVDA+AMZN) | Max DD |
|---|---:|---:|---:|---:|
| S0 current (stop) | +1,304% | −40% | +64% | −28% |
| N1 no stop + time stop | +2,174% | −45% | +170% | −28% |
| N2 no stop, TP only | +2,244% | −44% | +143% | −29% |
| B&H QQQ | +607% | −35% | +232% | −53% |
| B&H universe basket | +12,001% | −65% | +872% | −80% |

**Verdict: NOT ADOPTED (recorded for the decision).** No-stop raised returns for all six
strategies in both periods, but every held name in both tests eventually recovered (an
NVDA entry near the 2007 peak was held ~8 years). Losses become stuck slots: 2016–26
drawdowns rose for 5 of 6 strategies, and four of the five ensemble slots are underwater
today (ARM −40%). 2000–2002 is untested (IBKR 4h starts 2004). The 99% win rates and
t = 10–32 are artifacts: open losers never enter the trade statistics. 24 trials so far.


## Experiment: Wide disaster stop instead of no stop (2026-10-03)

Follow-up to the no-stop experiment. `research/no_stop_experiment.py --set disaster`:
stops at −20/−25/−30%, a 6-month max hold without a stop, and −25% + 6 months; TP and
the break-even time stop kept. 6 strategies × 2 periods, 5 new variants (54 trials
cumulative). Full write-up: [docs/disaster-stop-2004-2026.md](../docs/disaster-stop-2004-2026.md).

| ensemble @ $5k | 2016–26 | Max DD | 2022 | 2004–15 | Max DD | 2008 |
|---|---:|---:|---:|---:|---:|---:|
| S0 current (9%) | +1,304% | −40% | −28% | +64% | −28% | −23% |
| D20 | +1,634% | −43% | −33% | +155% | −31% | −24% |
| D25 | +2,183% | −47% | −40% | +126% | −30% | −21% |
| D30 | +2,482% | −49% | −42% | +178% | −31% | −19% |
| H6 (no stop, 6 mo) | +2,473% | −49% | −40% | +204% | −31% | −25% |

**Verdict: PROMISING, NOT ADOPTED.** All 60 wide-stop cells beat the current stop on
total return, and holds drop from years to under ~10 months. 2022 got worse for every
strategy, and no stop width is reliably best. H6 had −54%/−70% trades. Next: ensemble
with `ensemble_stop_loss_pct = 0.20` on the production engine, 2022–26 at $5k.
