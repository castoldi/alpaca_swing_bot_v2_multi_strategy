# Research Experiments Log

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
