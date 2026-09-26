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
