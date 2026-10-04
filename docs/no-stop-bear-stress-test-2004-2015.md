# No stop-loss: bear-market stress test, 2004–2015 (IBKR history)

**Date:** 2026-10-03 · **Script:** `research/no_stop_experiment.py --feed ibkr --start 2004-07-01 --end 2015-12-31` ·
**Raw output:** `research/results/no_stop_ibkr_<strategy>.txt` ·
**Main analysis:** [no-stop-2016-2026.md](no-stop-2016-2026.md)

## Why

The 2016–2026 test favoured removing the stop. But that decade had no lasting crash
in these names, so the recovery the no-stop variants depend on always came. The test
that matters is a real bear market.

## Data limits

- **The 2000–2002 crash cannot be tested on 4h bars.** IBKR serves hourly bars
  (the source for the 4h series) only from **January 2004** for NVDA and AMZN, and
  from **January 2015** for AMD, even though daily bars go back to 1999.
- Imported 2026-10-03: AMZN 4h (2004-01 →), AMD 4h + 1d (2015-01 →), QQQ and SPY 1d (1999 →).
  NVDA 4h was already in the cache. META (listed 2012) was not imported; ARM listed in 2023.
- **The tradable universe is therefore NVDA and AMZN** for 2004–2014, plus AMD in 2015.
  With 2 names and 5 slots of 20%, at most ~40% of the account is ever invested. That
  damps both gains and losses for every variant equally.
- The period still contains the **2008 bear**: NVDA fell about 85% peak to trough
  (2007–2008), AMZN about 65%, QQQ about 50%.
- The test starts 2004-07 so the 200-day indicator warmup is covered.

Method otherwise identical to the main analysis: same 3 variants fixed in advance,
one continuous $5,000 book, mark-to-market yearly returns, real portfolio engine.

## Results: ensemble and buy-and-hold

| Portfolio @ $5k | 2004* | 2005 | 2006 | 2007 | 2008 | 2009 | 2010 | 2011 | 2012 | 2013 | 2014 | 2015 | Total | CAGR | Max DD | End $ |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| ensemble S0 current | −17 | +11 | +17 | +12 | −23 | +29 | +6 | −5 | +1 | +10 | +1 | +22 | +64% | 4.4% | −28% | $7,925 |
| ensemble N1 no stop + time stop | −1 | +19 | +15 | +28 | −22 | +27 | +7 | +1 | +6 | +13 | −2 | +24 | +170% | 9.0% | −28% | $13,548 |
| ensemble N2 no stop, TP only | −1 | +14 | +14 | +24 | −23 | +28 | +5 | −1 | +7 | +13 | −2 | +25 | +143% | 8.0% | −29% | $12,607 |
| B&H QQQ | +8 | +2 | +7 | +19 | −42 | +55 | +20 | +3 | +18 | +37 | +19 | +11 | +232% | 11.0% | −53% | $16,610 |
| B&H SPY | +7 | +3 | +15 | +5 | −37 | +26 | +15 | +2 | +16 | +32 | +13 | +2 | +121% | 7.2% | −55% | $11,071 |
| B&H NVDA/AMZN | +0 | +36 | +62 | +53 | −68 | +148 | +7 | −5 | +23 | +55 | −7 | +105 | +872% | 21.9% | −80% | $48,590 |

\* 2004 = July–December only.

## Results: every bracket strategy

| Strategy | S0 current | N1 no stop + time stop | N2 no stop, TP only | 2008 S0 / N1 / N2 | Max DD S0 / N1 / N2 |
|---|---:|---:|---:|---:|---:|
| ensemble | +64% ($7.9k) | +170% ($13.5k) | +143% ($12.6k) | −23 / −22 / −23 | −28 / −28 / −29 |
| regime | +95% ($9.4k) | +139% ($12.1k) | +148% ($12.9k) | −15 / −23 / −23 | −22 / −29 / −29 |
| trend_pullback | +108% ($9.4k) | +144% ($12.3k) | +135% ($12.2k) | +1 / −15 / −23 | −13 / −27 / −29 |
| breakout | +6% ($5.4k) | +76% ($9.0k) | +118% ($11.2k) | −9 / −12 / −20 | −18 / −22 / −26 |
| momentum_macd | +22% ($5.8k) | +85% ($9.6k) | +98% ($10.7k) | −1 / −16 / −23 | −14 / −24 / −30 |
| mean_reversion | +26% ($6.4k) | +71% ($8.7k) | +98% ($10.2k) | +3 / +2 / −4 | −5 / −9 / −12 |

## Trade profile (ensemble)

| Variant | Trades | Win % | Worst closed trade | Median hold | Longest hold |
|---|---:|---:|---:|---:|---:|
| S0 current | 870 | 64% | −16.0% | 7 d | 76 d |
| N1 | 199 | 83%† | −10.8% (open) | 7 d | **2,942 d** |
| N2 | 90 | 98% | −10.8% (open) | 19 d | **2,961 d** |

† N1's non-winners are break-even time-stop exits at exactly 0%.

The ~2,960-day holds appear in every strategy (2,448–2,965 d for N2). Counting back
from late 2015 puts the entry around late 2007, and with only NVDA and AMZN tradable
(AMZN regained its 2007 high by 2009), it is an **NVDA position bought near the 2007
peak that took about eight years to reach its target**. N2 made only 65–123 trades
in 11.5 years for each strategy.

## Findings

1. **No-stop still beat the stop over 2004–2015 for every strategy.** Ensemble returned
   +143–170% vs +64%, and the worst drawdown was about the same (−28% vs −29%). The
   stopped version kept selling NVDA and AMZN into the 2008 decline and re-buying, and
   the stops cost more than the eventual recovery.
2. **2008 itself was no better without stops.** Ensemble lost −22% to −23% either way.
   For the steadier strategies the stop clearly protected 2008: trend_pullback +1% vs
   −15/−23%, momentum_macd −1% vs −16/−23%.
3. **Why the damage was bounded: position sizing, not the absence of a stop.** The
   eight-year stuck NVDA position only ever held one 20% slot. The 20% cap per position
   is what kept a −85% stock from sinking the account. With 5 names and all 5 slots stuck
   in a crash, the exposure would be up to 100%.
4. **Both tested names recovered.** NVDA took ~8 years, AMZN ~1. A name that never
   recovers would turn that slot into a permanent loss. Neither period tested that.
5. All buy-and-hold rows had −53% to −80% drawdowns. Every strategy variant stayed at
   −30% or better, mostly because 2 names in 5 slots meant 60% cash.

## Verdict

**Still not adopted.** Over 2004–2026, removing the stop raised returns in both
periods, and in 2008 the drawdown was no worse than with stops. The evidence has
three blind spots:

- **Survivorship.** Every name tested recovered. The variant's real risk, a holding
  that never comes back, is absent from both tests by construction.
- **2000–2002 is untested** (no 4h data before 2004).
- **Small universe.** 2 names in the stress test, so the account was never fully
  invested in a crash.

If this is pursued, the next trial should cap the tail it currently ignores: a wide
disaster stop (−25% to −30%) or a maximum hold (e.g. 6–12 months), measured on a
universe that includes names that failed to recover. Count it as further trials
(24 looked at so far).
