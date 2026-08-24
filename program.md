# Alpaca Swing Bot V2 — Autonomous Research Program

**System:** Multi-strategy swing trader on ALIENWARE 16 (RTX 5050 4GB, Intel i9)
**Universe:** NVDA · AMZN · META · AMD · ARM (same as V1)
**Live:** Alpaca paper trading, 20% of current equity per whole-share position, 5 positions max, no margin
**Dashboard:** http://192.168.0.191:8004 (give the user this, not `localhost` — they access it from their mobile network) — Home · Strategies · 2024/2025/2026 Reports

## Research Loop (autoresearch-inspired)

The agent follows this cycle autonomously:

1. **EXPLORE** — Propose a strategy modification or new signal in `strategy.py`
2. **BACKTEST** — Run `python backtest_2025.py` and `python backtest_2026.py`
3. **EVALUATE** — Compare results to baseline in SQLite DB
4. **KEEP or REVERT** — If P&L improves across both years, keep the change
5. **LOG** — Record the experiment in `research/experiments.md`
6. **REPEAT** — Start the next experiment

> ⚠️ **2026-08-24 — this loop's validation window is bull-only.** 2025 and 2026
> were both up years. Every "keep" decision ever made by steps 2–4 above was
> blind to bear markets. The bot's only losing year in 2016–2026 is **2022**
> (2018 was profitable), and it is not in the loop.
>
> For anything touching risk, drawdown, or regime, use
> `research/bear_market_experiment.py` instead — it scores all 11 years at once
> and reports bear/bull P&L separately. See
> [docs/bear-market-defence.md](docs/bear-market-defence.md).

## Research Goals (priority order)

1. **Cross-year consistency** — Any new strategy must be profitable in BOTH 2025 AND 2026
2. **Sharpe ratio > 1.5** — Risk-adjusted returns, not just raw P&L
3. **Max drawdown < 15%** — Capital preservation
4. **Win rate > 55%** — Consistency matters
5. **At least 20 trades/year** — Statistical significance

## Current baseline (all 7 strategies; annual $1,000 reset):

Each year starts with $1,000, uses whole-share positions capped at 20% of
realized equity, compounds gains and losses within that year, and resets before
the next year. The baseline also includes the breakeven-gated time stop and
four-day TP reachability filter.

| Strategy | 2024 P&L | 2025 P&L | 2026 P&L | 3-Year |
|----------|----------|----------|----------|--------|
| Trend Pullback | +$116.96 | +$87.46 | +$59.22 | +$263.64 |
| Breakout | +$63.26 | +$78.64 | +$3.95 | +$145.85 |
| Mean Reversion | -$7.40 | +$35.10 | -$23.15 | +$4.55 |
| MACD Momentum | +$90.73 | +$29.89 | +$3.99 | +$124.61 |
| **Ensemble** | **+$265.12** | **+$91.83** | **+$243.87** | **+$600.82** 🏆 |
| **Regime Adaptive** | **+$150.44** | **+$196.45** | **+$207.28** | **+$554.17** |
| **SMA 50 Cross (daily)** | **+$44.56** | **+$117.12** | **+$146.19** | **+$307.87** |

*Generated 2026-07-18 from Alpaca SIP bars. Each column is an independent
$1,000 annual account; 2026 is year-to-date through the latest completed bar.*

## Research ideas to explore:

- [x] ML signal combiner (weighted ensemble of all 5 strategies) — ✅ Rebalanced weights 2026-05-28: ensemble went from -$28.15 to +$194.44 (2025) and +$113.36 to +$319.11 (2026). Both years dramatically improved.
- [x] Tighter ensemble threshold (0.25→0.30) — ✅ 2026-05-28: ensemble P&L improved from +$194.44→+$199.92 (2025) and +$319.11→+$327.17 (2026). Both years improved, kept.
- [x] Revive Mean Reversion — ✅ 2026-05-28: Relaxed entry thresholds (rsi_oversold 48→50, deviation 0.01→0.005, BB mult 2.0→2.2). MR went from +$4.06 → +$12.43 combined with 20 trades (up from 13). Both years improved.
- [x] Market regime filter (Vix/SPY trend classifier) — ❌ 2026-06-01: VIX < SMA(20) filter on Breakout strategy. Both years worsened (2025: -$38.86 vs -$26.51, 2026: +$74.87 vs +$117.66). Reverted. VIX filtering alone doesn't help breakout on this universe.
- [ ] Adaptive position sizing (Kelly criterion)
- [ ] Multi-timeframe confirmation (1h + daily)
- [ ] Sector rotation overlay
- [x] Earnings-date avoidance filter — ✅ **2026-06-02: +$65.22 combined for Trend Pullback** (+126% 2025, +22% 2026). Skip entries 3 trading days before earnings to avoid gap risk. First *new signal source* (not parameter tweak) to pass cross-year test. Applied to Trend Pullback only — Breakout didn't benefit.
- [x] Daily SMA 50 price cross — ✅ **2026-07-18: +$655.55 across 2025–2026** on Alpaca daily bars. Long-only with a 10% emergency stop beat pure long-only, long/short reversal, and the shared TP/time-stop overlay in the option test. Added as an independent strategy without changing the six 4-hour strategies.
- [ ] Correlation-based drawdown protection
- [x] **Market-wide regime entry gate (SPY drawdown / SMA)** — ❌ **2026-08-24: REFUTED across 17 variants.** The gate recommended by `docs/bear-markets-and-crashes.md` §8 made 2022 *worse* (−$754 vs −$667) while cutting bull years −$3,654. It was ON for 72% of 2022 and 21% of 2023, suppressing the bear-market rallies (Mar/Jul/Nov 2022 were the year's most profitable months) and the entire 2023 recovery. Per-ticker gating, volatility-targeted sizing and stop widening all landed on the same tradeoff frontier. Full writeup: [docs/bear-market-defence.md](docs/bear-market-defence.md).
- [x] **Bear defence via strategy selection** — ✅ 2026-08-24: `breakout + tqqq_momentum` had **zero losing years 2016–2026** (8.7%/yr, worst year +1.4%) vs `ensemble` 24.9%/yr with a −16.9% 2022. It is an allocation decision, not a code change. Caveat: N=1 bear market.
- [ ] **Signal exits on fixed-bracket strategies** — highest-value untested idea. The strategies that survived 2022 (`tqqq_momentum`, `momentum_macd`) all "stay out" after an exit signal; the fixed-bracket ones re-enter and get stopped out repeatedly.
- [ ] **Stops ×1.5–2.0 as a pure return experiment** — +$734 to +$1,086 across 11 years in testing. Contradicts current tuning; unrelated to bear defence.

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
