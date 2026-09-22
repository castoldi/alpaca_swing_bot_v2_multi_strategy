# Corrected strategy baselines — 2026-09-21 (v0.25.1)

First baselines on the fully corrected engine: F01–F16, the V01–V09 follow-ups,
and the v0.25.1 significance fix. These replace every earlier backtest figure,
which the remediation marked historical pending revalidation.

**Engine:** IEX 4h signal bars (daily for `sma_50_cross`); fills at the first
one-minute regular-session open after the bar completes; stops that the price
gaps through fill at the open; stop-first when one minute touches both barriers;
exchange-session holding periods measured from the fill; daily-loss guard on
observable prices; 20% whole-share sizing, five slots, 20% leveraged cap; $1,000
reset each January. The earnings filter uses live observations from 2026-09-14
and, before that, the labelled historical import (dates assumed public 14 days
ahead). No spread, fee or latency model. Produced by `python backtest_2024.py`,
`backtest_2025.py`, `backtest_2026.py`; results are in the dashboard database and
`reports/backtest_20XX.html`.

## P&L on $1,000 per year

| Strategy | 2024 | 2025 | 2026 YTD | Trades 24/25/26 | Worst DD | Both 2025 & 2026 > 0 |
|---|---:|---:|---:|---|---:|:-:|
| **ensemble** (live) | **+$362.69** | **+$219.42** | **+$282.38** | 161 / 131 / 53 | 16.8% | ✅ |
| regime | +$246.84 | +$125.45 | +$156.87 | 173 / 135 / 52 | 17.5% | ✅ |
| sma_50_cross | +$37.06 | +$117.28 | +$154.28 | 32 / 17 / 15 | 5.7% | ✅ |
| trend_pullback | +$113.69 | +$30.77 | +$66.51 | 116 / 75 / 25 | 11.0% | ✅ |
| momentum_macd | +$47.85 | +$75.72 | +$16.68 | 35 / 20 / 5 | 4.3% | ✅ |
| mean_reversion | −$19.42 | +$22.98 | +$13.34 | 31 / 18 / 4 | 6.6% | ✅ (thin) |
| breakout | +$110.29 | −$75.85 | +$36.47 | 15 / 17 / 3 | 7.6% | ❌ |
| tqqq_momentum | −$12.90 | +$19.39 | −$14.71 | 18 / 17 / 20 | 3.7% | ❌ |

Drawdown here is the realized-equity measure the annual runner reports, which
misses unrealized dips (limitation 1 in the original review). Ensemble's 2022
year, from the ablation, was **−$232.67 (−23%) with a 31.8% drawdown**, so these
three bull-leaning years understate its risk.

## Multiple-testing verdicts (best-of-N per year)

| Year | Winner | t | Month-block hurdle | Verdict |
|---|---|---:|---:|---|
| 2024 | ensemble | 2.57 | 2.80 | fails |
| 2025 | ensemble | 1.89 | 3.43 | fails |
| 2026 | ensemble | 3.48 | 3.66 | fails (narrowly) |
| 2022–2026 pooled (4-variant ablation) | ensemble (live rule) | 4.13 | 2.48 | **clears** |

No single year's best strategy clears its own selection hurdle. Pooling five
years clears it, with the usual caveat that overlapping trades on correlated
tickers make per-trade t optimistic. These hurdles are finite only since
v0.25.1: before that fix every panel reported `t ≥ inf`.

## What changed versus the old figures

The old AGENTS.md figures for 2025 / 2026 were ensemble +$91.83 / +$243.87 and
regime +$196.45 / +$207.28. They came from the pre-remediation engine (signal-close
fills, no gap stops, bar-count holding, SIP data, broken RSI, Trend Pullback
suppressed), so none of these corrections can be attributed to a single change.

## Findings that matter for the live bot

- **The live Ensemble's entries are Regime's.** In the ablation, regime-only
  entries were identical to live across 2022–2026: the other four members never
  opened a trade on their own. The Ensemble beats plain `regime` because of its
  exit geometry (9% stop, 2.5×ATR target, 6 sessions), not because of the vote.
- The 2-vote rule roughly halves return and cuts bear-year damage and drawdown
  by about 60%. It is a candidate for a separately counted risk-objective
  experiment, not a return improvement. See Experiment 5 in `research/experiments.md`.
- `breakout` and `tqqq_momentum` fail the project's both-years rule on the
  corrected engine.
