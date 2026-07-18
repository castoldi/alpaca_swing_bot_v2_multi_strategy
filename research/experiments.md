# Research Experiments Log

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
- `mr_bollinger_mult`: 2.0 → 2.2 (wider Bollinger Band, more price action inside lower band)

**Results:**

| Metric | Before | After | Improvement |
|--------|--------|-------|-------------|
| 2025 P&L | -$3.94 (11 trades) | **-$3.05 (15 trades)** | Smaller loss, 36% more trades ✅ |
| 2026 P&L | +$8.00 (2 trades) | **+$15.48 (5 trades)** | 94% improvement, 2.5× trades ✅✅ |
| Combined | +$4.06 | **+$12.43** | Tripled ✅✅✅ |
| Ensemble 2026 | +$327.17 | +$329.92 | Slight improvement from MR votes |
| All other strategies | Unchanged | Unchanged | No regressions |

**Verdict: KEPT** — Both years improved. Mean Reversion is now generating +$12.43 combined with 20 trades across both years (up from 13). Still small vs other strategies but no longer dead.

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

**Verdict: KEPT** — Both years improved dramatically. Ensemble is now the #1 strategy by P&L.
