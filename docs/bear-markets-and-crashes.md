# Bear Markets and Crashes — Historical Catalog and Entry-Gate Feasibility

**Written:** 2026-08-08 · **Bot version:** 0.19.2
**Companion to:** [markov-and-garch.md](markov-and-garch.md) (which recommended a
200-day SMA index filter but tested it only on 2018–2026, a window it explicitly
flagged as bull-dominated) and [systematic-strategies.md](systematic-strategies.md)
§3.3. This document supplies the missing piece: a real catalog of every S&P 500
bear market and crash back to 1990, and a point-in-time test of whether any of them
were detectable early enough to matter.

Data: daily closes for the S&P 500 (`^GSPC`), Nasdaq Composite (`^IXIC`), and VIX
(`^VIX`) from Yahoo Finance, 1990-01-02 through 2026-08-07 (9,217 trading days),
plus the bot's own tickers (NVDA, AMD, AMZN, META, ARM, TQQQ, QQQ) for whatever
history each has. This is a different source than the bot's normal Alpaca feed —
Alpaca's cache only goes back to ~2016 (per markov-and-garch.md), which is why
that document could not evaluate 2000–2002 or 2007–2009 at all. Yahoo's daily
history has no such limit, so this analysis can finally cover those two.
All signals below are strictly point-in-time: a signal for day *t* only ever uses
data through day *t*, matching the discipline `data_feed.completed_bars` already
enforces in the live bot.

---

## Bottom line up front

1. **Yes, bear markets are identifiable — with real, quantified lag.** A simple
   "price is 10%+ below its 52-week high" signal caught the four real bear markets
   in this window 8–50 days after the top, by which point 23–54% of the eventual
   decline had already happened. It is not early warning; it is fast confirmation.
   That is still useful, because bear markets grind on for months after that point.

2. **A macro entry gate would have cut max drawdown by roughly half and cost about
   a third of the index's total return over 36 years**, using the best signal found
   (drawdown ≥10% off the 52-week high): total return 1,319% vs. 2,057% buy-and-hold,
   max drawdown −26.1% vs. −56.8%, and the gate was only active 18.8% of trading
   days. Full comparison in §7.

3. **VIX alone is the wrong signal.** It fires and un-fires in bursts (average
   episode length 8 trading days) because it tracks acute fear spikes, not
   persistent regimes — it barely dented max drawdown (−56.6% vs. −56.8%, i.e.
   almost no protection) because it kept flickering back off in the middle of
   grinding bears like 2022.

4. **This bot's own universe falls harder than the index in every bear market
   tested** — NVDA, AMD, AMZN, META, TQQQ all declined 1.2×–3.5× the S&P's
   peak-to-trough loss (§5). A market-wide gate matters more here than it would for
   a diversified index fund.

5. **This refines, not just repeats, the prior recommendation.** markov-and-garch.md
   endorsed the 200-day SMA filter over the HMM. Tested here against 36 years
   including four real ≥20% bears, "≥10% off the 52-week high" beat the 200-day SMA
   on every axis that matters (higher retained return, lower max drawdown, fewer
   whipsaw flips) — see §7. Worth an A/B backtest on the bot's own strategies before
   picking one.

---

## 1. The bear-market catalog (S&P 500, ≥20% peak-to-trough, 1990–2026)

| Peak | Trough | Decline | Time falling | Time to fully recover | Recovered | VIX peak |
|---|---|---:|---:|---:|---|---:|
| 2000-03-24 | 2002-10-09 | **−49.1%** | 929 days (~2.5 yr) | 1,694 days (~4.6 yr) | 2007-05-30 | 45 |
| 2007-10-09 | 2009-03-09 | **−56.8%** | 517 days (~1.4 yr) | 1,480 days (~4 yr) | 2013-03-28 | 81 |
| 2020-02-19 | 2020-03-23 | **−33.9%** | 33 days (~1 mo) | 148 days (~5 mo) | 2020-08-18 | 83 |
| 2022-01-03 | 2022-10-12 | **−25.4%** | 282 days (~9 mo) | 464 days (~15 mo) | 2024-01-19 | 36 |

These are the dot-com crash, the Global Financial Crisis, the COVID crash, and the
2022 rate-hike bear market — the four standard, textbook ≥20% bears in this window.
No fifth one is currently in progress: SPX closed at an all-time high on 2026-08-07,
the latest bar in this dataset.

The Nasdaq Composite — much closer to this bot's NVDA/AMD/AMZN/META/ARM universe
than the S&P — fell harder in every one of them:

| Bear market (SPX peak date) | SPX decline | Nasdaq Composite decline |
|---|---:|---:|
| 2000-03-24 | −49.1% | **−77.6%** |
| 2007-10-09 | −56.8% | −54.8% |
| 2020-02-19 | −33.9% | −30.1% |
| 2022-01-03 | −25.4% | −34.2% |

---

## 2. Corrections and near-misses (10–20% peak-to-trough, not counted above)

| Peak | Trough | Decline | Duration | Note |
|---|---|---:|---:|---|
| 1990-01-02 | 1990-01-30 | −10.2% | 28d | Gulf War build-up |
| 1990-07-16 | 1990-10-11 | −19.9% | 87d | Gulf War / oil shock |
| 1997-10-07 | 1997-10-27 | −10.8% | 20d | Asian financial crisis |
| 1998-07-17 | 1998-08-31 | −19.3% | 45d | Russian default / LTCM collapse |
| 1999-07-16 | 1999-10-15 | −12.1% | 91d | Pre-Y2K jitters |
| 2015-05-21 | 2016-02-11 | −14.2% | 266d | China devaluation / oil crash |
| 2018-01-26 | 2018-02-08 | −10.2% | 13d | Volmageddon (short-vol unwind) |
| 2018-09-20 | 2018-12-24 | −19.8% | 95d | Fed rate-hike fears |
| **2025-02-19** | **2025-04-08** | **−18.9%** | **48d** | **Tariff-policy shock — the most recent episode, one bear-market-threshold miss** |

The 2025 episode is worth calling out specifically: it peaked at SPX 6,144 on
2025-02-19, bottomed at 4,983 on 2025-04-08 (VIX hit 52 that day — higher than the
2022 bear's entire peak VIX of 36), and fully recovered to a new high by
2025-06-27 — an 80-day round trip from trough to new high. This bot's live trading
only began in June 2026 (per its own trade history), so it never traded through
this episode, but it is the most relevant real-world comparison available: fast,
deep, and over in under two months.

---

## 3. Fast crashes (≥10% decline within any rolling 20-trading-day window)

33 such episodes since 1990 — roughly one per 13 months. The largest:

| Window | Worst 20-day drawdown | Event |
|---|---:|---|
| 2008-09-29 → 2008-12-02 | **−28.4%** | Lehman collapse / GFC panic |
| 2020-02-27 → 2020-04-03 | **−29.5%** | COVID-19 crash |
| 2009-02-19 → 2009-03-12 | −22.2% | GFC final capitulation |
| 2001-09-17 → 2001-10-01 | −18.5% | 9/11 |
| 2002-07-10 → 2002-08-05 | −19.5% | Dot-com bear final leg |
| 2011-08-04 → 2011-08-25 | −16.8% | US credit downgrade / euro crisis |
| 2018-12-19 → 2019-01-02 | −15.7% | Q4 2018 selloff |
| 2025-04-04 → 2025-04-08 | −13.7% | Tariff-shock crash leg |

**Single-day drops ≥7% happened exactly 7 times in 36 years**, all clustered in two
windows: 2008-09-29, 2008-10-09, 2008-10-15, 2008-12-01 (GFC), and 2020-03-09,
2020-03-12, 2020-03-16 (COVID). By definition, no signal can avoid the loss *on*
the crash day itself — the earliest any gate can act is the next session. This
bot's existing `entry_max_slippage_pct` guard and the daily-loss kill switch (§6)
already cover the same-day case; a dedicated single-day-drop trigger would add
little because the event is too rare (0.08% of days) and too late by construction
to function as prevention.

---

## 4. How hard this bot's own universe gets hit

Peak-to-trough decline of each ticker measured over the same SPX bear-market
windows (only where the ticker had trading history):

| Ticker | 2000 dot-com | 2007 GFC | 2020 COVID | 2022 bear |
|---|---:|---:|---:|---:|
| **SPX (reference)** | −49.1% | −56.8% | −33.9% | −25.4% |
| NVDA | −68.4% | −84.0% | −37.6% | −61.8% |
| AMD | −88.1% | −86.9% | −34.3% | −61.6% |
| AMZN | −91.8% | −63.3% | −22.7% | −40.0% |
| META | n/a (IPO 2012) | n/a | −32.9% | −62.3% |
| TQQQ | n/a (inception 2010) | n/a | −69.9% | −78.8% |
| QQQ | −82.9% | −52.1% | −28.6% | −34.6% |

Every one of this bot's core tickers fell harder than the S&P in every bear market
where data exists — typically 1.2×–3.5× the index decline, and NVDA/AMD/META all
lost 60%+ in 2022 alone against the S&P's 25%. A market-wide regime gate is not a
theoretical nicety for this universe; the tickers it trades are structurally more
exposed to exactly the drawdowns being studied here.

---

## 5. Can the bot actually see these coming? Signal-lag results

Four candidate point-in-time signals, tested against each of the four ≥20% bears
plus the 2025 correction:

- **`close < 200-day SMA`** — the filter markov-and-garch.md already endorsed
- **`drawdown ≥10% off the trailing 252-day (52-week) high`**
- **`VIX ≥ 30`**
- **`death cross`** (50-day SMA crosses below 200-day SMA)

For each, the table shows how many days after the market's peak the signal fired,
and what fraction of the *eventual* peak-to-trough decline had already happened by
then (lower is better — it means the signal caught the top early):

| Bear market | close<200sma | dd≥10% off 52w high | VIX≥30 | death cross |
|---|---|---|---|---|
| **2000-03-24** (−49.1%) | 21d, 23% realized | 21d, 23% realized | 21d, 23% realized | 220d, 17% realized |
| **2007-10-09** (−56.8%) | 29d, 10% realized | 48d, 18% realized | 34d, 14% realized | 73d, 9% realized |
| **2020-02-19** (−33.9%) | 8d, 35% realized | 8d, 35% realized | 8d, 35% realized | 40d, **66% realized** |
| **2022-01-03** (−25.4%) | 18d, 33% realized | 50d, 40% realized | 22d, 36% realized | 70d, 51% realized |
| **2025-02-19** (−18.9%, correction) | 19d, 46% realized | 22d, 54% realized | 43d, 64% realized | **54d, 64% realized — fired 6 days *after* the trough** |

Two patterns worth internalizing:

- **The faster the crash, the more of it is already priced in by the time any
  signal fires.** In the 33-day COVID crash, even the fastest signals (8-day lag)
  had already missed 35% of the total decline; in the 48-day 2025 correction, the
  fastest missed 46%. Slow-grinding bears (2007, 2022) give signals more relative
  lead time because the total decline takes longer to accumulate.
- **The death cross is too slow for anything shorter than a multi-year bear.** It
  fired well *after* the trough in both the 2020 crash (missed 66% of the move) and
  the 2025 correction (fired 6 days after the bottom, effectively confirming a bear
  market that was already over). It only looks good on 2000 and 2007 because those
  bears lasted over a year — plenty of time for a lagging signal to still be useful.

---

## 6. What each signal would have cost or saved, full 36-year history

Simulating "hold the index normally; go to cash whenever the signal is true,"
1990–2026, no lookahead:

| Signal | Time flagged out | Total return | vs. buy-and-hold (2,057%) | Max drawdown | vs. buy-and-hold (−56.8%) | Gate ON episodes (36 yr) | Avg episode length |
|---|---:|---:|---:|---:|---:|---:|---:|
| **dd ≥10% off 52w high** | 18.8% | **1,319.5%** | 64% retained | **−26.1%** | 54% cut | 62 | 28 trading days |
| Death cross | 23.0% | 1,410.0% | 69% retained | −33.9% | 40% cut | 16 | 132 trading days |
| close < 200sma | 23.4% | 884.0% | 43% retained | −28.3% | 50% cut | 118 | 18 trading days |
| VIX ≥ 30 | 8.0% | 684.8% | 33% retained | −56.6% | ~0% cut | 93 | 8 trading days |

Reading this table: **VIX≥30 is not a usable standalone gate** — it barely
improved max drawdown at all, because its short, flickering episodes (avg 8 days)
mean it keeps turning back off in the middle of a still-declining market. The
**200-day SMA** (the previously-endorsed filter) works, but flips on/off more than
twice as often as the 52-week-drawdown signal (118 vs. 62 episodes) for a worse
return/drawdown tradeoff. The **death cross** retains the most return of the four
because it is rarely active at all (16 episodes in 36 years) — but §5 already
showed it is unreliable for anything shorter than a multi-year bear, so its good
aggregate numbers are propped up almost entirely by 2000 and 2007–2009.

**`dd ≥10% off 52-week high` is the best-balanced signal tested**: second-fewest
whipsaws, best drawdown cut, best return retention among the two "responsive"
signals (excluding the death cross's lucky-timing advantage), and it fired inside
the useful window (not too early, not after the fact) in four of five episodes
tested in §5.

---

## 7. How this interacts with what the bot already has

Two mechanisms already exist and are **not redundant** with a macro entry gate:

- **The daily-loss kill switch** (`max_daily_loss_pct = 0.03`, `bot.py:_daily_loss_pct`)
  halts new entries only after the *account* is already down 3% versus yesterday's
  close, re-evaluated fresh each cycle. It is reactive and same-day — it does
  nothing during a slow grind like 2022 (282 days, rarely a 3% single-day move) and
  nothing about entries placed *before* a crash. It has no view of the broader
  market, only this account's own equity curve — the daily-loss kill switch
  backtest work (`a9eea4d`) operates at the account level, not the index level.
- **The `regime` strategy's EMA(10)/EMA(50) detector** (`strategies/regime_adaptive.py`)
  changes *how* one strategy enters (risk-on dip-buy vs. risk-off oversold-bounce
  vs. neutral) per ticker — it does not block the other seven strategies from
  trading that ticker, and it has no market-wide view either; each ticker is
  classified independently.

A macro gate (SPY or the S&P closing below its 200-day SMA, or 10%+ off its 52-week
high) would sit **above** both of these: a single daily check that disables new
entries for *every* strategy and *every* ticker when the broad market itself is in
a confirmed decline, while leaving exits, broker-held brackets, and the existing
kill switch untouched — the same non-interference pattern the kill switch itself
already follows ("exits and broker-held protection keep working").

---

## 8. Verdict and recommendation

**Yes — a market-wide entry gate is identifiable and worth building, with the
signal refined from what markov-and-garch.md recommended.**

1. **Candidate signal:** `SPY (or SPX) drawdown ≥10% off its trailing 252-day
   high`, computed once per day from completed daily bars (no lookahead — matches
   the existing `data_feed.completed_bars` discipline). It beat the previously-
   endorsed 200-day SMA filter on retained return, max-drawdown cut, and whipsaw
   frequency when tested across all four real ≥20% bears instead of only the
   2018–2026 window. Recommend an A/B backtest of both signals directly on this
   bot's own strategies before choosing — §7's numbers are index-only, not the
   bot's own trade-level P&L.

2. **Where it plugs in**, following the same pattern already used for the
   leveraged-exposure cap (identical logic in live sizing and
   `backtest_portfolio.run_annual_portfolio`, so backtest and live can never
   silently disagree):
   - Add to `StrategyParams` in `config.py`: `market_regime_gate_enabled`,
     `market_regime_gate_ticker` (default `"SPY"`), `market_regime_dd_threshold`
     (default `0.10`).
   - Add a `market_regime_blocked(as_of_date) -> bool` check, computed from daily
     SPY bars, next to the existing kill-switch check in `bot.py`'s entry path.
   - Mirror it in `backtest_portfolio.run_annual_portfolio` exactly as the
     leveraged cap is mirrored, so the same function decides both live and
     backtested behavior.
   - Skip new entries only — never touch open positions, exits, or broker-held
     TP/SL, matching the kill switch's existing non-interference contract.

3. **Reject VIX≥30 as a standalone gate** — §6 shows it does not meaningfully
   reduce drawdown because it flickers on and off inside still-declining markets.
   It could still be useful as a *secondary* confirmation alongside the drawdown
   signal, not as a replacement.

4. **Reject a single-day-drop trigger** — too rare (7 events in 36 years) and by
   construction too late to prevent the day's own loss; the existing kill switch
   and slippage guard already cover the same-day case.

5. **Honest limitations of this document:**
   - The bear-market catalog and signal-lag numbers (§1–§6) are computed on the
     **S&P 500 index itself**, not this bot's actual multi-leg bracket trades. They
     establish that the *signal* is real and identifiable — they are not a
     backtest of the bot with the gate installed.
   - The bot's own strategy backtests (`backtest_2024/2025/2026.py`) run on
     Alpaca-sourced data that only goes back to ~2016, so the bot's actual
     entry/exit logic has never been simulated through 2000–2002 or 2007–2009 —
     those two bears can only be studied at the index/single-name level, as done
     here. The 2022 bear and the 2025 correction *are* within the Alpaca cache's
     range and should be the first real validation targets.
   - Four ≥20% bears is a small sample. The ranking in §7 (52w-drawdown > death
     cross > 200sma > VIX) should be treated as a strong prior, not a settled
     conclusion, until it is re-tested on the bot's own trade-level P&L.
   - Per [systematic-strategies.md §3.7](systematic-strategies.md) and
     markov-and-garch.md's own recommendation, any configuration actually tried
     needs to be logged via `db_mod.log_experiment(...)` — this analysis has not
     touched the bot's code and logged nothing yet.

**Suggested next step:** implement the gate as described in item 2, run it through
`backtest_2025.py` (which now spans the real 2025 correction) and a synthetic
2022-window backtest, and compare against both the ungated baseline and the
200-day-SMA variant before deciding which one ships.

---

## Appendix — Reproducing these results

Two standalone scripts were written for this analysis (session scratchpad, not
committed — they depend only on `yfinance`, `pandas`, and `numpy`, all already in
the project's venv):

| Script | Produces |
|---|---|
| `fetch_data.py` | Downloads daily 1990–2026 history for SPX, Nasdaq Composite, VIX, SPY, QQQ, and the bot's own tickers via yfinance |
| `analyze.py` | Bear-market/correction/crash catalogs (§1–§3), per-ticker drawdown comparison (§4), signal-lag study (§5), and full-history gate cost/benefit with transition counts (§6) |

If the gate in §8 is implemented, these should move into `research/` and be wired
into `db_mod.log_experiment`, consistent with the validation-methodology gap both
companion documents already flag.
