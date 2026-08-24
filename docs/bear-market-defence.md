# Can this bot avoid losing money in bear markets and still make money in bull markets?

**Written:** 2026-08-24 · **Method:** autoresearch-style experiment loop (17 variants, 3 independent mechanisms)
**Companion to:** [bear-markets-and-crashes.md](bear-markets-and-crashes.md), which catalogued the bears and
recommended a market-wide entry gate. This document **tests that recommendation on the bot's own trade-level
P&L and refutes it**, then finds what does work.

---

## Bottom line up front

1. **Yes — it is possible, and it costs about 65% of the return.** Two strategy
   combinations had **zero losing years across 2016–2026**, including the 2022
   bear: `breakout + tqqq_momentum` (8.7%/yr, worst year **+1.4%**) and
   `breakout + momentum_macd + tqqq_momentum` (7.5%/yr, worst year **+1.1%**).
   The current flagship, `ensemble`, earns 24.9%/yr but lost **−16.9%** in 2022.

2. **The answer is strategy selection, not a bear-market filter.** All 17 filter
   variants tested failed. Every mechanism that reduced the 2022 loss reduced
   bull-market profit by more — usually far more.

3. **The pre-registered entry gate is refuted.** `bear-markets-and-crashes.md` §8
   recommended blocking entries when SPY is ≥10% off its 252-day high, chosen on
   1990–2026 index data. On the bot's own trades it made 2022 **worse**
   (−$754 vs −$667) while cutting bull years by **−$3,654**. It is not a
   near-miss; it is backwards.

4. **The bot's only losing year in 11 is 2022.** 2018 — the other index-down year
   — was **+$557**. So this whole question rests on a single episode. Treat every
   conclusion here as N=1 evidence.

5. **`breakout` alone is the standout single strategy**: 12.6%/yr (half of
   ensemble) with a worst year of **−3.0%** (one-sixth of ensemble's drawdown).

---

## 1. The baseline: where the bot actually loses

Per-year P&L, all 8 strategies, each on an independent $1,000 annual account
(Alpaca SIP bars, 2016 → 2026-08; 2026 is year-to-date):

| Strategy | 11y total | avg/yr | worst year | losing years | 2022 | 2018 |
|---|---:|---:|---:|---:|---:|---:|
| ensemble | +$2,743 | 24.9% | −16.9% | 1 | −$169 | +$176 |
| regime | +$2,676 | 24.3% | −27.5% | 1 | −$275 | +$193 |
| trend_pullback | +$1,958 | 17.8% | −16.1% | 2 | −$161 | −$10 |
| sma_50_cross | +$1,684 | 15.3% | −12.1% | 1 | −$121 | +$145 |
| breakout | +$1,391 | 12.6% | **−3.0%** | 1 | −$30 | +$99 |
| momentum_macd | +$564 | 5.1% | **−0.3%** | 1 | **+$49** | −$3 |
| tqqq_momentum | +$532 | 4.8% | **−0.4%** | 1 | **+$60** | −$4 |
| mean_reversion | +$214 | 1.9% | −2.8% | 5 | −$28 | −$12 |

**2022 is the only meaningfully losing year for any strategy.** 2018 (SPX −6.2%,
two corrections) was profitable for six of eight. So "bear market" here means
2022 and essentially nothing else.

### Why 2022 loses — full attribution

`research/diagnose_2022.py`, 824 trades:

| Exit reason | Trades | P&L |
|---|---:|---:|
| **stop_loss** | 186 | **−$2,555.56** |
| sma_cross_down | 21 | −$113.16 |
| take_profit | 112 | +$746.53 |
| time_stop | 475 | +$1,185.63 |
| ema_break | 28 | +$74.56 |

The entire loss is stop-losses. Everything else is net positive. And by entry
month:

| Losing months | | Winning months | |
|---|---:|---|---:|
| Feb | −$279 | **Nov** | **+$244** |
| Jan | −$184 | **Jul** | **+$226** |
| Jun | −$173 | **Mar** | **+$174** |
| Oct | −$160 | | |

March, July and November 2022 — the three bear-market *rally* months — were the
bot's most profitable months of the year. **This is the fact that kills every
entry gate**: those rallies all happened while SPY was still >10% below its
January high, so any drawdown-based gate was switched on straight through them.

---

## 2. Hypothesis 1 — market-wide entry gate (REFUTED)

Implemented in `market_regime.py` exactly as
[bear-markets-and-crashes.md](bear-markets-and-crashes.md) §8 specified: block
new entries only, never touch exits or open positions, point-in-time (a decision
for bar *t* uses only SPY daily bars that closed strictly before *t*'s date).

| Variant | bear P&L | bull P&L | 2022 | 2023 | verdict |
|---|---:|---:|---:|---:|---|
| baseline | −$110 | +$11,839 | −$667 | +$2,728 | — |
| market: dd ≥10% off 52w high | −$115 | +$8,190 | **−$754** | **+$628** | ❌ |
| market: close < 200d SMA | −$93 | +$8,946 | −$732 | +$1,820 | ❌ |
| market: close < 50d SMA | −$207 | +$8,109 | −$474 | +$1,595 | ❌ |
| ticker: close < 200d SMA | −$166 | +$8,525 | −$540 | +$1,174 | ❌ |
| ticker: close < 50d SMA | **+$182** | +$9,033 | −$413 | +$2,386 | ❌ |
| ticker: dd ≥10% | +$285 | +$3,979 | −$188 | +$437 | ❌ |
| ticker: dd ≥20% | +$53 | +$6,850 | −$302 | +$732 | ❌ |

The gate was ON for **72% of 2022 — and 2022 still got worse.** Blocking nearly
three-quarters of the year's entries increased the loss, because the entries it
blocked were the profitable rally trades while the stop-outs it did let through
remained.

It was also still ON for **21% of 2023**, which is why the single best year in
the sample collapsed from +$2,728 to +$628. The signal is a lagging one: SPY did
not reclaim its January-2022 high until January 2024, so a 52-week-drawdown gate
suppressed the entire 2023 recovery.

Best bear-market improvement of any gate (`ticker: dd ≥10%`, +$479 in 2022) cost
**$7,860** in bull years — a 16:1 loss ratio. The least-bad (`ticker: sma50`)
was still ~11:1.

> **Why the index study didn't transfer.** `bear-markets-and-crashes.md` measured
> a buy-and-hold index holder, for whom going to cash genuinely avoids the
> drawdown. This bot is a 3–7 day swing trader with 7–12% stops that profits from
> volatile bounces. Going to cash removes its edge rather than protecting it.
> The index-level signal was real; the assumption that it transfers to this
> trading style was wrong.

## 3. Hypothesis 2 — volatility-targeted sizing (REFUTED, but closer)

Keep every signal; scale size by `clip(target_vol / realized_20d_vol, lo, hi)`.
Bear markets are high-volatility, so size shrinks with no regime prediction.
Realized vol across signals: p25 = 0.28, median = 0.40, p75 = 0.55, p95 = 0.90.

| Variant | bear P&L | bull P&L | 2022 | verdict |
|---|---:|---:|---:|---|
| baseline (flat 20%) | −$110 | +$11,839 | −$667 | — |
| target 0.40, clip ≤1.0 | +$27 | +$8,734 | −$451 | ❌ |
| target 0.50, clip ≤1.0 | −$1 | +$9,859 | −$520 | ❌ |
| target 0.45, clip ≤1.3 | −$44 | +$11,480 | −$576 | ❌ (best ratio) |
| target 0.50, clip ≤1.5 | −$194 | +$13,090 | −$661 | ❌ |

Better than gating — `target 0.45 / clip ≤1.3` removed 60% of the aggregate bear
loss for only a 3% bull cost (5.4:1) — but it still never gets 2022 to
break-even, and the variants that *improve* total return do so purely by taking
**larger** positions in calm markets (clip >1.0). That is added leverage, not
risk management, and it makes 2022 worse. Not an answer to the question asked.

## 4. Hypothesis 3 — wider stops (REFUTED, informative)

Since 100% of the 2022 loss is stop-outs, and time-stop exits *made* $1,186,
widening stops should convert whipsaw stop-outs into time-stop gains. Every
`*_stop_loss_pct` scaled uniformly:

| Variant | bear P&L | bull P&L | 2022 | total |
|---|---:|---:|---:|---:|
| stops ×0.75 | −$162 | +$10,314 | −$556 | +$10,152 |
| **baseline ×1.0** | −$110 | +$11,839 | −$667 | +$11,729 |
| stops ×1.25 | −$197 | +$12,317 | −$816 | +$12,120 |
| stops ×1.5 | −$486 | +$12,949 | −$998 | +$12,463 |
| stops ×2.0 | −$823 | +$13,638 | −$1,039 | +$12,815 |

Perfectly monotonic in both directions: **wider stops make more money overall and
lose more in the bear; tighter stops do the reverse.** The whipsaws were real
losses, not noise to be waited out. (×2.0 raising total return by +$1,086 is a
separate finding worth its own test — but it is the opposite of bear defence.)

---

## 5. What actually works: strategy selection

Equal-weight combinations, capital-normalised (running *N* strategies deploys
*N* × $1,000, so raw sums are not comparable — these divide by *N*):

| Portfolio | 11y P&L | avg/yr | worst year | losing years | 2022 |
|---|---:|---:|---:|---:|---:|
| ensemble | +$2,743 | **24.9%** | −16.9% | 1 | −16.9% |
| regime | +$2,676 | 24.3% | −27.5% | 1 | −27.5% |
| ensemble + tqqq_momentum | +$1,638 | 14.9% | −5.5% | 1 | −5.5% |
| ALL 8 | +$1,470 | 13.4% | −8.4% | 1 | −8.4% |
| breakout | +$1,391 | 12.6% | −3.0% | 1 | −3.0% |
| **breakout + tqqq_momentum** | +$962 | **8.7%** | **+1.4%** | **0** | **+1.5%** |
| **breakout + momentum_macd + tqqq_momentum** | +$829 | **7.5%** | **+1.1%** | **0** | **+2.6%** |

Two portfolios never had a losing year in 11 years. The efficient frontier:

```
avg/yr   worst yr   portfolio
24.9%     -16.9%    ensemble                      ← current default
14.9%      -5.5%    ensemble + tqqq_momentum      ← 60% of return, 1/3 the drawdown
12.6%      -3.0%    breakout                      ← best single strategy
 8.7%      +1.4%    breakout + tqqq_momentum      ← never loses
 7.5%      +1.1%    breakout + macd + tqqq        ← never loses
```

The two bear-proof ingredients share a structural property the losers lack:
`tqqq_momentum` and `sma_50_cross`-style **signal exits that stay out**.
`tqqq_momentum` exits on a 4h close below EMA(50) and does not re-enter until TSI
crosses back up — so in a downtrend it simply stops trading, without anyone
having to classify the regime. The fixed-bracket strategies re-enter on every
setup and get stopped out repeatedly. `momentum_macd` benefits similarly from
requiring a fresh MACD cross plus price above both SMAs.

Note `sma_50_cross` *has* a signal exit yet still lost −$121 in 2022: the exit
kept it out of the worst of it, but its 21 trades that year were mostly losers
(14% win rate). Signal exits reduce bear damage; they do not eliminate it.

---

## 6. Honest limitations

- **N=1.** 2022 is the only real bear market in Alpaca's data range, and 2018 was
  profitable. Every claim here — the successes and the 17 failures — rests on one
  episode. `tqqq_momentum` returning +6.0% in 2022 could be luck.
- **The refutation is more robust than the recommendation.** 17 variants across 3
  independent mechanisms all landing on the same tradeoff frontier is strong
  evidence that no cheap filter exists. "These two portfolios never lose" is much
  weaker — it is a survivor selected from 8 candidates on 11 observations.
- **Capital fragmentation is not modelled.** The combination rows assume each
  strategy gets a clean share of capital. In reality, splitting $1,000 two or
  three ways means $333–500 sub-accounts, where whole-share sizing skips
  high-priced stocks entirely (a documented pitfall). Real combination results
  would be **worse than shown**.
- 2026 is year-to-date through 2026-08-21.
- Pre-2016 bears (2000, 2007–09) remain untestable at trade level — Alpaca's
  history does not reach them.

---

## 7. Recommendation

**Do not implement the market-regime entry gate** from
`bear-markets-and-crashes.md` §8. It is refuted on this bot's own P&L. The
supporting code (`market_regime.py`, `regime_gate=` and `position_fraction_fn=`
in `run_annual_portfolio`) is left in place, default-off, as research tooling.

If bear protection is wanted, it is a **capital allocation decision, not a code
change**:

- **Keep maximum return:** run `ensemble` and accept a −17% year in a bear.
- **Balanced (recommended):** `ensemble + tqqq_momentum` — 60% of the return,
  drawdown cut from −16.9% to −5.5%.
- **Never lose a year:** `breakout + tqqq_momentum` — 8.7%/yr, +1.4% worst year,
  at the cost of ~65% of the upside.

Next tests worth running, in priority order:

1. **Signal exits on the fixed-bracket strategies.** The one structural property
   that separates 2022's winners from its losers. Test an EMA-break exit added to
   `ensemble` — the highest-value untested idea in this document.
2. **Stops ×1.5–2.0 as a pure return experiment** (+$734 to +$1,086 across 11
   years). Nothing to do with bear defence; it just appears to be free money and
   contradicts current tuning.
3. **Walk-forward validation** of the two "never lose" portfolios, since they
   were selected on the same 11 years they are scored on.
