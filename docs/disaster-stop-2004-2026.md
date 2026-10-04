# Wide disaster stop instead of no stop, 2004–2026

**Date:** 2026-10-03 · **Script:** `research/no_stop_experiment.py --set disaster` (add
`--feed ibkr --start 2004-07-01 --end 2015-12-31` for the stress test) ·
**Raw output:** `research/results/disaster_<strategy>.txt`, `research/results/disaster_ibkr_<strategy>.txt` ·
**Follows:** [no-stop-2016-2026.md](no-stop-2016-2026.md),
[no-stop-bear-stress-test-2004-2015.md](no-stop-bear-stress-test-2004-2015.md)

## Question

Removing the stop made more money, but only because every held stock recovered, and
it left positions stuck for years. Does a **wide** stop keep most of that upside while
capping the stock that never comes back? A −25% stop on a 20% position caps any one
loss at about 5% of the account.

## Variants (fixed before any result was seen)

All keep the take-profit and the current break-even time stop (exit after N sessions
only if at or above entry).

| Code | Exit rule |
|---|---|
| S0 | current per-strategy stop (7–12%), reference |
| N1 | no stop, reference (from the no-stop analysis) |
| D20 / D25 / D30 | stop at −20% / −25% / −30% from the fill |
| H6 | no stop, but sell at the close once 126 sessions (~6 months) have passed |
| D25+H6 | −25% stop plus the 6-month limit |

5 new variants × 6 strategies = 30 trials, **54 cumulative** in this line of research.
Same harness as the no-stop analysis: one continuous $5,000 book, real portfolio
engine, mark-to-market drawdowns, legacy 4h engine. S0 and N1 were re-run with the new
exit loop and **reproduced the earlier numbers exactly**.

## Results: total return / worst drawdown

**2016–2026 (Alpaca SIP, 5 names)**

| Strategy | S0 current | N1 no stop | D20 | D25 | D30 | H6 | D25+H6 |
|---|---:|---:|---:|---:|---:|---:|---:|
| ensemble | +1,304% / −40% | +2,174% / −45% | +1,634% / −43% | +2,183% / −47% | +2,482% / −49% | +2,473% / −49% | +2,158% / −46% |
| regime | +1,017% / −50% | +2,125% / −46% | +1,277% / −50% | +1,999% / −51% | +2,219% / −51% | +2,432% / −49% | +1,994% / −50% |
| trend_pullback | +640% / −38% | +1,895% / −47% | +1,001% / −42% | +1,538% / −45% | +1,359% / −48% | +1,621% / −54% | +1,545% / −45% |
| momentum_macd | +144% / −20% | +477% / −41% | +185% / −29% | +263% / −31% | +240% / −34% | +331% / −46% | +263% / −31% |
| breakout | +162% / −12% | +458% / −44% | +312% / −33% | +280% / −37% | +249% / −35% | +332% / −42% | +276% / −37% |
| mean_reversion | +17% / −13% | +215% / −35% | +67% / −24% | +67% / −25% | +88% / −25% | +108% / −29% | +67% / −25% |

**2004–2015 (IBKR, NVDA + AMZN, AMD from 2015; includes 2008)**

| Strategy | S0 current | N1 no stop | D20 | D25 | D30 | H6 | D25+H6 |
|---|---:|---:|---:|---:|---:|---:|---:|
| ensemble | +64% / −28% | +170% / −28% | +155% / −31% | +126% / −30% | +178% / −31% | +204% / −31% | +122% / −30% |
| regime | +95% / −22% | +139% / −29% | +153% / −30% | +172% / −25% | +172% / −23% | +168% / −34% | +165% / −25% |
| trend_pullback | +108% / −13% | +144% / −27% | +178% / −16% | +160% / −15% | +146% / −18% | +183% / −27% | +159% / −15% |
| momentum_macd | +22% / −14% | +85% / −24% | +65% / −10% | +64% / −13% | +64% / −15% | +79% / −25% | +67% / −13% |
| breakout | +6% / −18% | +76% / −22% | +68% / −11% | +57% / −14% | +55% / −11% | +60% / −22% | +57% / −14% |
| mean_reversion | +26% / −5% | +71% / −9% | +43% / −6% | +41% / −7% | +43% / −8% | +63% / −9% | +41% / −7% |

## The bad years: 2022 and 2008

| 2022 | S0 | N1 | D20 | D25 | D30 | H6 | D25+H6 |
|---|---:|---:|---:|---:|---:|---:|---:|
| ensemble | −28% | −40% | −33% | −40% | −42% | −40% | −40% |
| regime | −40% | −40% | −41% | −44% | −41% | −39% | −43% |
| trend_pullback | −22% | −41% | −30% | −32% | −38% | −44% | −32% |
| momentum_macd | −11% | −35% | −25% | −27% | −30% | −38% | −27% |
| breakout | −10% | −40% | −30% | −35% | −33% | −36% | −35% |
| mean_reversion | −9% | −32% | −21% | −23% | −20% | −27% | −23% |

| 2008 | S0 | N1 | D20 | D25 | D30 | H6 | D25+H6 |
|---|---:|---:|---:|---:|---:|---:|---:|
| ensemble | −23% | −22% | −24% | −21% | −19% | −25% | −21% |
| regime | −15% | −23% | −20% | −16% | −12% | −26% | −16% |
| trend_pullback | +1% | −15% | −2% | −9% | −9% | −15% | −9% |
| momentum_macd | −1% | −16% | +2% | −1% | −3% | −15% | −1% |
| breakout | −9% | −12% | −5% | −8% | −5% | −12% | −8% |
| mean_reversion | +3% | +2% | +5% | +4% | +3% | +2% | +4% |

## Trade profile (ensemble)

| Variant | 2016–26 trades | Win % | Worst trade | Longest hold | 2004–15 worst trade | Longest hold |
|---|---:|---:|---:|---:|---:|---:|
| S0 current | 2,049 | 77% | −13.6% | 53 d | −16.0% | 76 d |
| N1 no stop | 610 | 98% | −35.9% (open) | 865 d | −10.8% (open) | 2,942 d |
| D20 | 1,298 | 91% | −25.0% | 214 d | −22.6% | 153 d |
| D25 | 1,124 | 93% | −27.7% | 214 d | −28.6% | 195 d |
| D30 | 1,043 | 95% | −30.0% | 225 d | −31.7% | 295 d |
| H6 | 832 | 95% | **−54.2%** | 186 d | **−70.5%** | 185 d |
| D25+H6 | 1,131 | 93% | −27.7% | 183 d | −28.6% | 182 d |

Worst trades below the stop level are gaps through the stop (filled at the open).

## Findings

1. **Every wide-stop variant beat the current stop on total return: all 6 strategies,
   both periods, 60 of 60 cells.** The current 7–12% stops are too tight for these
   names. They sell dips that recover.
2. **The wide stops fix the "stuck for years" problem.** The longest hold falls from
   865–2,942 days (no stop) to 153–295 days for ensemble, and the worst loss per trade is bounded
   at about the stop level (≈ 4–6% of the account at 20% sizing), except for gaps.
3. **But 2022 got clearly worse for every strategy.** Ensemble went from −28% (current)
   to −33% (D20) and −40% (D25). The deepest drawdown 2016–2026 rose by 3–9 points for
   ensemble and by 9–25 points for the steadier strategies (momentum_macd −20% → −29%
   to −34%, breakout −12% → −33% to −37%). In 2008 the effect was mixed: within ±5
   points for most strategies, but worse for trend_pullback (+1% → −2% to −9%).
4. **The 6-month time limit without a stop (H6) is the worst idea**: highest returns
   in some cells but the worst trades (−54%, −70%). D25+H6 behaves like D25 because
   the stop almost always fires before 6 months, so the time limit adds nothing.
5. **No stop width is reliably best.** D20/D25/D30 swap places across strategies and
   periods (ensemble 2004–15: D20 +155%, D25 +126%, D30 +178%), so the exact number
   is noise. The robust part is "wider than 7–12%".
6. **For the live ensemble, D20 is the balanced choice.** It gains 2016–26 +1,634% vs
   +1,304% and 2004–15 +155% vs +64%, at a cost of about 3 points more drawdown and
   2022 −33% vs −28%.

## Caveats

- **Same universe bias as before.** These 5 names were picked with hindsight. The wide
  stop's whole job is the name that never recovers, and that case is still absent.
- **Legacy 4h engine.** The production engine (minute fills, earnings filter, kill
  switch, annual reset) has given numbers up to ~40 points a year different. Nothing
  here was run on it.
- **54 trials.** The high per-trade t-statistics are not meaningful evidence, for the
  same reason as before: the consistency across strategies is the evidence, and those
  strategies share entries.
- 2000–2002 is still untested (no 4h data before 2004).

## Verdict

**Promising, not adopted yet.** The consistent result is that the current stop is too
tight, not that the stop should go. Recommended next step before any live change:
re-run **ensemble with `ensemble_stop_loss_pct = 0.20`** (now 0.09) on the
production engine, 2022–2026 at $5,000 (`research/capital_size_experiment.py` harness),
and compare it with the current +250% / −35% (2022) / 40.6% max drawdown. Adopt only if
it still wins there and the operator accepts a deeper 2022-type year.
