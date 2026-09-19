# Bear-market playbook — how badly this bot loses, what doesn't fix it, what might

**Written:** 2026-09-19 · **Supersedes the "what to do" parts of:**
[bear-market-defence.md](bear-market-defence.md) (2026-08-24, the 17-filter study) and
[bear-markets-and-crashes.md](bear-markets-and-crashes.md) (2026-08-08, the index catalog).
Those two stay as the detailed evidence; this is the one page to read first.

**Question asked:** this bot makes money buying pullbacks. How do we stop it buying
pullbacks that keep falling in a bear market, and should it short rallies instead?

---

## Bottom line

1. **Fast crashes don't hurt this bot; slow grinding bears do.** `ensemble` made
   **+42.9% in the 2020 COVID crash** and +10.2% in the 2025 tariff crash, then
   lost **−16.9% in the 2022 bear** (−22% peak-to-trough inside the year). 2022
   is its only losing year of the 11 it can be tested on.
2. **Identifying the bear market is not the hard part — acting on it is.**
   Every detector tested (21 variants, 5 families: price trend, drawdown,
   volatility, credit, breadth, VIX term structure) **made 2022 worse** or cost
   far more in good years than it saved in 2022. Blocking entries also blocks the
   violent bear-market rallies, which were the bot's *best* months of 2022.
3. **Shorting rallies in a confirmed bear has no edge** in 28 years of data
   (76 trades, +0.2%/trade, t = 0.26). It won in 2000–01, lost in 2008 and
   squeezed in the rebounds. With unlimited upside risk and a margin account
   required, it is not worth building. Details in §4.
4. **What actually reduces bear losses is portfolio shape, not a switch.** Pair
   `ensemble` with `tqqq_momentum` (worst year −16.9% → −5.5%, keeps ~60% of the
   return) — a config decision, available today.
5. **The best untested idea is a trend-break exit** on the fixed-bracket
   strategies: the two strategies that *made* money in 2022 both exit on a
   trend break instead of waiting for a 9–10% stop. See §5, idea B.

---

## 1. How bad is it — by episode

Each cell is one calendar year on an independent $1,000 account, real Alpaca SIP
4h bars, 5 tickers (NVDA AMZN META AMD ARM) — `reports/backtest_2016_present.json`.

| Strategy | 2018 (−20% Q4) | 2020 COVID crash | **2022 bear** | 2025 tariff crash | 2022 max drawdown |
|---|---:|---:|---:|---:|---:|
| ensemble (live default) | +17.6% | +42.9% | **−16.9%** | +10.2% | −22.0% |
| regime | +19.3% | +27.7% | **−27.5%** | +22.3% | −32.0% |
| trend_pullback | −1.0% | +37.8% | −16.1% | +6.3% | −21.7% |
| sma_50_cross | +14.5% | +21.6% | −12.1% | +11.7% | −12.1% |
| breakout | +9.9% | +24.1% | −3.0% | +7.2% | −6.7% |
| momentum_macd | −0.3% | +8.9% | **+4.9%** | +3.0% | — |
| tqqq_momentum | −0.4% | +4.5% | **+6.0%** | +18.3% | −2.6% |

**Where 2022's money went** (`research/diagnose_2022.py`, 824 trades): stop-losses
−$2,556; *every other exit type was net positive* (time stops +$1,186, take-profits
+$747). The bot doesn't lose because its entries are random in a bear — it loses
because it **re-enters every setup in a downtrend and gets stopped out repeatedly
at −9 to −10% each**. The winning months were March, July and November 2022 —
the three bear-market rallies.

**What we cannot measure yet:** 2000–02 (Nasdaq −78%) and 2007–09 (S&P −57%).
This universe fell 1.2–3.5× harder than the S&P in every bear with data (NVDA
−84% and AMD −87% in 2008). Alpaca history starts in 2016; the IBKR import
(`scripts/import_ibkr_history.py`, feed `ibkr`, 1999+) now makes a trade-level
backtest of those two possible — that is the most important next test (§6).

---

## 2. Detectors tested — all fail

Scored on the bot's own trades, all 8 strategies, 2016–2026. "Bull" = the 9
non-bear years; "Bull years" is the change vs baseline (2022 −$667; bull +$11,839 in the Aug-24 runs, +$11,970 in the 2026-09-19 runs, whose cache had three more weeks of 2026).

| Family | Variant | On (% days) | 2022 | Bull years | Verdict |
|---|---|---:|---:|---:|---|
| Price (SPY) | ≥10% off 52-week high | — | −$754 | −$3,649 | ❌ worse both |
| Price (SPY) | close < 200d SMA | — | −$732 | −$2,893 | ❌ |
| Price (SPY) | close < 50d SMA | — | −$474 | −$3,729 | ❌ 19:1 cost |
| Price (ticker) | ticker < its 50d SMA | — | −$413 | −$2,806 | ❌ 11:1 cost |
| Volatility | vol-targeted size (best) | — | −$576 | −$359 | ❌ 2022 still loses |
| Stops | stops ×0.75 / ×1.5 / ×2.0 | — | monotonic | monotonic | ❌ tighter = less bull |
| **VIX term structure** | VIX > VIX3M (inverted) | 7.5% | **−$837** | −$1,358 | ❌ new, 2026-09-19 |
| **Credit** | HYG/IEF < its 200d SMA | 25% | **−$949** | −$4,894 | ❌ new |
| **Breadth** | < 4 of 9 sector ETFs > 200d | 12.5% | **−$813** | −$2,402 | ❌ new |
| **Combined** | 2 of the 3 above | 10% | **−$779** | −$1,877 | ❌ new |

(The four new rows ran on commit `d70a195` so the baseline matches the Aug-24
study exactly; the script is `research/external_gate_experiment.py`.)

**Why they all fail, in one sentence:** each detector is late *and* stays on
through the bear-market rallies, so it removes the profitable rebound trades and
leaves the losing stop-outs it didn't catch in time. Breadth, for example, was
correctly ON for 47% of 2022 — and 2022 still got worse. The credit signal
barely fired in 2022 at all because Treasuries fell alongside junk bonds.

The index-level study (bear-markets-and-crashes.md) showed these signals *do*
protect a buy-and-hold index investor. They don't transfer to a 3–7-day swing
trader whose edge is volatile bounces.

---

## 3. Crashes are a different animal

2020 and 2025 were the bot's strong years *because* of the crash. A 33-day crash
has no time for a regime detector to fire (every signal was 8+ days late with
35%+ of the fall already done), and the V-shaped rebound is exactly the "buy the
panic dip" trade the bot is built for. Whatever is done about grinding bears must
**not** switch the bot off in crashes — which rules out anything keyed to VIX
spikes or fast drawdowns.

Same-day protection already exists: the −3% daily-loss kill switch and the 1.5%
entry-slippage guard.

---

## 4. Should the bot short rallies in a bear market?

**Test** (`research/short_the_rally_experiment.py`, rules fixed before results):
daily bars 1998–2026, NVDA AMZN AMD META QQQ, three real bears. Mirror image of
`trend_pullback`: short when price is below its 50d SMA, RSI(14) rallied above 55
in the last 3 bars, and the day closes down; 10% stop, 2×ATR target [3–8%],
5-day time stop, 0.10% round-trip cost. "Confirmed bear" = SPY below a *falling*
200d SMA (on for 73% of 2000–02, 78% of 2008, 55% of 2022).

| | Trades | Win | Avg/trade | Profit factor | t-stat (4 trials) |
|---|---:|---:|---:|---:|---:|
| **Short, confirmed bear** | 76 | 54% | **+0.20%** | 1.07 | **0.26 — fails** |
| Short, not bear | 342 | 52% | +0.27% | 1.13 | — |
| Long (buy dip), confirmed bear | 68 | 63% | **+2.20%** | 2.17 | **2.85 — clears** |
| Long (buy dip), not bear | 430 | 52% | −0.26% | 0.88 | — |

By episode, short side in the confirmed-bear regime:

| Bear | Short trades | Avg | Result |
|---|---:|---:|---|
| 2000–02 dot-com | 21 | +2.6% | ✅ worked |
| 2007–09 GFC | 12 | −1.2% | ❌ lost (squeezes: Mar & Dec 2008) |
| 2022 rate hikes | 6 | +1.4% | ✅ small |
| Regime on outside a real bear: 2011, 2015, 2019, 2020, 2023 | 16 | −0.2% to −8.1% | ❌ all five years lost |
| Regime on outside a real bear: 2016 | 7 | +3.8% | ✅ the one exception |

**Verdict: don't build it.**

- **No edge.** +0.2% per trade with t = 0.26 is noise. It works in one bear, fails
  in another, and loses in five of six false-alarm years — which is when the
  market snaps back hardest.
- **Bear rallies are the most violent moves in markets.** The worst trade gapped
  straight through the stop for −16.5% (AMD, Oct 2002). A long's loss is capped at
  −100%; a short's is not, and a gap can blow past any stop order.
- **Mechanics conflict with the bot's rules.** Shorting needs a margin account
  (Alpaca: ≥$2,000 equity, borrow availability, locate fees on hard-to-borrow
  names). CLAUDE.md says **no margin**; the paper account is shared by 9 projects.
- **If short exposure is ever wanted anyway**, buy an inverse ETF (`SQQQ`, `SOXS`)
  or a put instead of shorting stock: loss capped at the amount paid, no margin, no
  borrow, no unlimited-loss squeeze. It would be a long-only trade the existing
  bracket machinery already knows how to protect. Inverse leveraged ETFs decay in
  choppy markets, so they fit a `tqqq_momentum`-style signal exit, not a 5-day
  bracket.

**The unexpected finding** is the long row: buying pullbacks *during* a confirmed
bear, but only in names still above their own 50d SMA (relative strength),
averaged +2.2%/trade and clears the multiple-testing hurdle. That is the
opposite of the intuition — the risk is not "buying in a bear", it's buying
**the names that are breaking down**. Caveats: 68 trades, overlapping and
correlated, daily bars rather than the bot's 4h, and 2022 alone was bad for it
(5 trades, −26%). It is a lead to test, not a result to trade (§5, idea C).

---

## 5. Solutions, ranked

| # | Idea | Status | Cost to upside | Why it might work |
|---|---|---|---|---|
| **A** | **Allocate: `ensemble + tqqq_momentum`** | backtested | ~40% of return | worst year −16.9% → −5.5%. Config only. |
| **B** | **Trend-break exit on bracket strategies** (exit on 4h close < EMA(50), don't wait for the 9–10% stop) | **untested — top priority** | unknown | Attacks the real cause (repeated stop-outs). The only two 2022 winners both exit this way. |
| **C** | **Relative-strength filter only when the market is in a confirmed bear** (skip entries in names below their own 50d SMA *while* SPY < falling 200d) | untested | small (regime on ~18% of days) | §4 long result; differs from the failed always-on ticker filter because it's off in bull years. |
| **D** | **Equity drawdown brake**: halve position size when the account is >10% below its peak; restore at a new high | untested | small in bull | Keys off the bot's *own* losses, so it can't be fooled by bear rallies the way index signals are. |
| **E** | **Cap consecutive stop-outs per ticker** (after 2 stops in 20 days, skip that ticker 10 days) | untested | small | Targets the exact 2022 pattern: re-entering a falling name over and over. |
| F | Inverse-ETF hedge (`SQQQ` via a signal exit) | not recommended yet | decay + drag | Only if §4-style shorting is revisited; capped risk. |
| ✗ | Market-wide entry gate (any detector) | **refuted** (21 variants) | 3–16× the savings | §2 |
| ✗ | Short-selling rallies | **refuted** (§4) | — | No edge, unlimited risk, needs margin |
| ✗ | Wider or tighter stops | refuted | — | Monotonic trade-off, no free lunch |

**Rules for testing B–E** (research-loop discipline, CLAUDE.md):
- Score with `research/bear_market_experiment.py` (bear vs bull P&L across all 11
  years), not the 2025/2026 two-year check — that one is blind to bears.
- Count trials honestly. This page already spent **25** (17 + 4 + 4). A variant of
  B with three EMA lengths is three more.
- Pass only if 2022 improves **and** bull years lose less than 2022 gains —
  and then re-check on 2000–02 / 2007–09 with the IBKR data before believing it.

---

## 6. Recommended plan

1. **Now, no code:** decide on allocation A. It is the only measured improvement.
2. **Next test:** idea B (EMA-break exit) on `ensemble`, then E and D. C last,
   since it rests on a daily-bar result.
3. **Then validate on real bears:** once `import_ibkr_history.py` has finished,
   run the whole portfolio on 2000–02 and 2007–09 with `feed="ibkr"`. That turns
   every conclusion here from N=1 (2022) into N=3.
4. **Don't build:** market entry gates, short selling.

---

## 7. Limits of this evidence

- **N=1 at trade level.** 2022 is the only bear the bot's own backtest covers.
  §4 reaches 3 bears but on daily bars with a simplified strategy.
- **Per-trade statistics are optimistic** — trades overlap in time across
  correlated tickers.
- **Combination results ignore capital fragmentation** (whole-share sizing on
  split capital skips expensive names), so real portfolio results are worse.
- The four new gates ran on `d70a195`; the uncommitted F15 execution-model changes
  now require 1-minute data that doesn't exist before 2016, so the current tree
  can't run 2016–2019.
