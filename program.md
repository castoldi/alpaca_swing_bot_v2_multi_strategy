# Alpaca Swing Bot V2 — Autonomous Research Program

**System:** Multi-strategy swing trader on ALIENWARE 16 (RTX 5050 4GB, Intel i9)
**Universe:** NVDA · AMZN · META · AMD · ARM (same as V1)
**Live:** Alpaca paper trading, $200/trade, $1,000 cap
**Dashboard:** http://localhost:8004 (LAN: http://192.168.0.191:8004) — Home · Strategies · 2024/2025/2026 Reports

## Research Loop (autoresearch-inspired)

The agent follows this cycle autonomously:

1. **EXPLORE** — Propose a strategy modification or new signal in `strategy.py`
2. **BACKTEST** — Run `python backtest_2025.py` and `python backtest_2026.py`
3. **EVALUATE** — Compare results to baseline in SQLite DB
4. **KEEP or REVERT** — If P&L improves across both years, keep the change
5. **LOG** — Record the experiment in `research/experiments.md`
6. **REPEAT** — Start the next experiment

## Research Goals (priority order)

1. **Cross-year consistency** — Any new strategy must be profitable in BOTH 2025 AND 2026
2. **Sharpe ratio > 1.5** — Risk-adjusted returns, not just raw P&L
3. **Max drawdown < 15%** — Capital preservation
4. **Win rate > 55%** — Consistency matters
5. **At least 20 trades/year** — Statistical significance

## Current baseline (all 6 strategies, as of 2026-06-02):

Changes since last baseline: breakeven-gated time stop (no hard time stop — only exits at time stop if position is profitable), TP reachability filter raised from days=2 to days=4 (fixed breakout and momentum_macd being blocked by miscalibrated filter).

| Strategy | 2024 P&L | 2025 P&L | 2026 P&L | 3-Year |
|----------|----------|----------|----------|--------|
| Trend Pullback | +$196.16 | +$12.30 | +$82.17 | +$290.63 |
| Breakout | +$26.73 | -$8.04 | +$114.66 | +$133.35 |
| Mean Reversion | +$69.16 | -$5.39 | +$20.17 | +$83.94 |
| MACD Momentum | +$55.59 | +$19.81 | +$16.92 | +$92.32 |
| **Ensemble** | **+$340.02** | **+$180.87** | **+$308.00** | **+$828.89** 🏆 |
| **Regime Adaptive** | **+$335.67** | **+$173.75** | **+$233.92** | **+$743.34** |

## Research ideas to explore:

- [x] ML signal combiner (weighted ensemble of all 5 strategies) — ✅ Rebalanced weights 2026-05-28: ensemble went from -$28.15 to +$194.44 (2025) and +$113.36 to +$319.11 (2026). Both years dramatically improved.
- [x] Tighter ensemble threshold (0.25→0.30) — ✅ 2026-05-28: ensemble P&L improved from +$194.44→+$199.92 (2025) and +$319.11→+$327.17 (2026). Both years improved, kept.
- [x] Revive Mean Reversion — ✅ 2026-05-28: Relaxed entry thresholds (rsi_oversold 48→50, deviation 0.01→0.005, BB mult 2.0→2.2). MR went from +$4.06 → +$12.43 combined with 20 trades (up from 13). Both years improved.
- [x] Market regime filter (Vix/SPY trend classifier) — ❌ 2026-06-01: VIX < SMA(20) filter on Breakout strategy. Both years worsened (2025: -$38.86 vs -$26.51, 2026: +$74.87 vs +$117.66). Reverted. VIX filtering alone doesn't help breakout on this universe.
- [ ] Adaptive position sizing (Kelly criterion)
- [ ] Multi-timeframe confirmation (1h + daily)
- [ ] Sector rotation overlay
- [x] Earnings-date avoidance filter — ✅ **2026-06-02: +$65.22 combined for Trend Pullback** (+126% 2025, +22% 2026). Skip entries 3 trading days before earnings to avoid gap risk. First *new signal source* (not parameter tweak) to pass cross-year test. Applied to Trend Pullback only — Breakout didn't benefit.
- [ ] Correlation-based drawdown protection

## Fixed experiments:
- [x] **simulate_exit bug — uses signal.stop_loss directly** — ✅ 2026-05-31: Known pitfall fixed. `simulate_exit()` now uses `signal.stop_loss`/`signal.take_profit` directly instead of recalculating from strategy params. This makes backtest consistent with live bot behavior. Regime impact: 2025 −$4.13, 2026 −$0.52 (within noise).
- [x] **Breakeven-gated time stop** — ✅ 2026-06-02: Replaced hard time stop with a conditional one — position only exits at the time-stop bar if `close >= entry_price` (breaking even or better). If underwater, holds until SL or TP. Prevents locking in losses at the time stop while still freeing the ticker when trades stall at a profit.
- [x] **TP reachability filter: days=2 → days=4** — ✅ 2026-06-02: Bug fix. Breakout (TP at 3×ATR) and momentum_macd (TP at 2.5×ATR) were always blocked by the old `days=2` filter (which requires TP within 2 ATR-movements). Raising to `days=4` restores both strategies. Breakout: 0 trades → 23/22/13 per year. MACD: 0 → 21/23/9 per year.

| Experiment | Change | 2025 | 2026 | Verdict |
| | | | | |
| Breakout MACD filter | Require macd_hist>0 on entry | -$10.51 vs -$8.67 | +$117.66 vs +$117.66 | ❌ reverted |
| Regime ATR vol filter 1.5× | Skip entries when ATR% > 1.5× avg | unchanged (zero filter hits) | unchanged | ❌ reverted |
| Regime ATR vol filter 1.2× | Skip entries when ATR% > 1.2× avg | -$2.23 vs +$89.82 | unchanged | ❌ reverted (catastrophic) |
| Breakout TP reduction | 3.0×→2.5× ATR, cap 15%→12% | -$59.69 vs -$26.51 | +$117.66 vs +$117.66 | ❌ reverted (catastrophic) |
| MACD hold 6→8 days | Increase max holding days | +$34.19 vs +$13.31 | +$25.30 vs +$47.80 | ❌ reverted (2026 worse) |
| MR remove SMA50 filter | Removed close>sma_slow uptrend requirement | -$53.51 vs -$26.31 | +$103.61 vs +$26.68 | ❌ reverted (let in 76 bad trades in 2025, 93 vs 17 total trades) |
| Breakout VIX filter | VIX < SMA(20) filter — skip during elevated VIX | -$38.86 vs -$26.51 | +$74.87 vs +$117.66 | ❌ reverted (both years worse, VIX alone doesn't filter breakout quality) |
| **Earnings avoidance filter** 🚀 | Skip entries 3 trading days before earnings (Trend Pullback only) | **+$71.37 vs +$31.62 (+$39.76)** | **+$142.02 vs +$116.55 (+$25.47)** | **✅ KEPT — +$65.22 combined** |

**Lesson update 2026-06-02**: The earnings avoidance filter is the FIRST experiment (out of 8) that passed the cross-year test with a genuine improvement. Key difference: it's a *new signal source* (earnings calendar data), not a parameter tweak or volatility filter. This validates the hypothesis that future research should focus on external data sources rather than indicator parameters. Remaining untested ideas: Kelly criterion sizing, multi-timeframe confirmation, sector rotation, correlation-based drawdown protection.