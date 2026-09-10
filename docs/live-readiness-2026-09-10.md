# Live-account readiness assessment

**Written:** 2026-09-10 · **Version at time of audit:** 0.24.7 · **Strategy running:** `ensemble` @30m
**Question:** is this bot ready to trade a real-money account?
**Scope note:** a separate Alpaca account is **explicitly out of scope** for now (operator decision,
2026-09-10). The plan below achieves capital isolation *inside* the shared paper key instead.

---

## Verdict

**No.** Three independent reasons, in order of severity:

1. **The live track record is not a measurement of this strategy.** The account is shared with 8 other
   projects. The bot's own ledger and the broker disagree by **$54,400**. Four trades were closed by
   another project. Nothing in the +$7,779.94 can be cleanly attributed here.
2. **The stop-loss has already failed twice, and both times it was the largest loss of the period.**
   META exited at −12.31% against a −9.0% stop; AMZN at −9.86%. A broker stop does not survive a gap,
   and `ensemble` has no earnings filter.
3. **The one bear market in the data loses 16.9%**, the research says no cheap filter fixes it, and the
   fix that *was* identified has neither been adopted nor is it runnable on the current process model.

Additionally, the reported statistics are corrupted by duplicate rows, and every parameter in the
project was tuned at a capital level that understates drawdown by ~3.5×.

---

## 1. The live paper record (2026-06-25 → 2026-09-09)

| Metric | Value |
|---|---:|
| Trade rows in DB | 67 |
| **Unique** (ticker + entry_date + entry_price) | **43** |
| Closed trades | 64 |
| Realized P&L | **+$7,779.94** |
| Open positions / cost basis | 3 / $63,572.86 |
| Bot ledger equity | $161,690.37 |
| **Broker equity** | **$107,290.32** |

### 1.1 P&L by exit reason — the designed exit loses money

| Exit reason | Trades | P&L |
|---|---:|---:|
| `time_stop` | 23 | **+$11,041.24** |
| `bracket_filled` | 35 | **−$2,319.63** |
| `protection_breached` | 1 | −$878.34 |
| `external_liquidation` | 4 | −$63.33 |
| `entry_not_filled` | 1 | $0.00 |

Split further:

- `bracket_filled`: 27 "wins" totalling **+$66.53**, 8 losses totalling **−$2,386.16**
- `time_stop`: 21 wins **+$11,049.42**, 2 losses −$8.18

Every dollar of profit comes from the time stop. The bracket — the TP/SL pair that is supposed to be
the core risk management — is **net negative across 35 trades**.

### 1.2 Profit is three trades

| Ticker | Exit | P&L |
|---|---|---:|
| ARM (09-02 → 09-08) | time_stop | +$2,480.38 |
| NVDA (08-03 → 08-06) | time_stop | +$1,495.41 |
| META (07-31 → 08-03) | time_stop | +$1,463.17 |
| **Subtotal** | | **+$5,438.96 (70% of all P&L)** |

64 trades over 2.5 months, in a strong mega-cap tech tape.

---

## 2. Blockers

### 2.1 Capital and attribution are shared — CRITICAL

`bot.py:197-235` (`_load_live_sizing`) reads **account-wide** equity and cash, with an explicit comment
that this is deliberate. Only the position-*slot* count is scoped to this bot's own trades.

Consequence: at `position_size_pct = 0.20` and `max_concurrent_positions = 5`, this bot will size
**5 × 20% = 100% of the entire shared account**. Currently it holds $63,572.86 against a real broker
equity of $107,290.32 — **59% of an account eight other projects also draw on.**

The ledger drift follows from the same root: `balance_history` carries `starting_capital = $154,114.89`
(a stale snapshot including other projects' capital), producing a reported equity of $161,690.37 against
a real $107,290.32.

Four trades already closed as `external_liquidation` (−$63.33) — another project flattening this bot's
positions. Per project memory, a day-trader bot on the same key does an EOD flatten.

### 2.2 Duplicate trade rows corrupt every statistic — CRITICAL

| Ticker | Entry | Rows |
|---|---|---:|
| NVDA | 2026-07-02 16:00 @ 194.51 | **22** |
| NVDA | 2026-06-29 16:00 @ 194.92 | 3 |
| META | 2026-09-04 16:00 @ 616.75 | 2 |

24 excess rows out of 67. `client_order_id` is unique on every row, so these are distinct orders or
per-fill records — not a pure display bug. The dashboard's "48 wins / 15 losses" and the 83% win rate
are both inflated. **No performance claim from this DB is currently trustworthy.**

### 2.3 Stops have been breached — CRITICAL

| Trade | Entry | Stop | Actual exit | Loss |
|---|---:|---:|---:|---:|
| META (id 40), 07-27 → 07-30 | 601.025 | 546.93 (−9.0%) | 527.01 (**−12.31%**) | −$2,384.58 |
| AMZN (id 45), 08-03 → 09-08 | 285.32 | 259.64 (−9.0%) | 257.19 (**−9.86%**) | −$878.34 |

The META trade spans an earnings print. **`ensemble` never calls `add_earnings_filter`** — the
machinery exists (`strategies/base.py:60`, `config.py:230` `earnings_avoid_days = 3`) but only
`trend_pullback` checks `near_earnings` (`strategies/trend_pullback.py:28`).

At 20% sizing this is the dominant tail risk on real money: a stop is a limit on *intent*, not on loss.

### 2.4 The breakeven-gated time stop holds losers indefinitely — HIGH

9 of 64 trades were held past the 6-day `ensemble` max hold; those 9 total **−$525.91**.

- AMD: entered 08-04, exited 09-09 — **36 days** — at +0.04% (+$8.71)
- AMZN: entered 08-03, exited 09-08 — **36 days** — at −9.86%

The gate holds a losing position until it recovers to breakeven. That manufactures a high win rate out
of many near-zero exits while leaving the losses uncapped in time. It is the disposition effect
implemented in code. The 83% headline win rate is a direct product of it.

### 2.5 Tuning was done at a capital level that isn't the one running — HIGH

`config.py:76` `initial_backtest_equity = 1000.0`, `position_size_pct = 0.20` → a **$200** per-position
budget with whole-share sizing. At that budget META (~$600), AMD (~$520), AMZN (~$285) and ARM (~$265)
are skipped entirely — the documented "high-priced stocks" pitfall. The backtests are largely NVDA-only.

Controlled re-run of 2026 `ensemble`, identical signals, varying only starting capital:

| Capital | 20% budget | Trades | Win% | P&L | Return | PF | Max DD |
|---:|---:|---:|---:|---:|---:|---:|---:|
| $1,000 | $200 | 115 | 86.1% | +$322.42 | 32.24% | **2.54** | **4.6%** |
| $5,000 | $1,000 | 252 | 79.4% | +$1,070.05 | 21.40% | 1.25 | 14.1% |
| $25,000 | $5,000 | 252 | 79.4% | +$6,038.09 | 24.15% | 1.25 | 15.9% |
| $107,000 | $21,400 | 252 | 79.4% | +$25,500.83 | 23.83% | **1.24** | **16.4%** |

Return survives (~24%). **Risk does not.** The $1,000 backtest takes less than half the trades and
understates drawdown by **3.5×**. Every parameter decision in this project was made against the top row.

### 2.6 No multiple-testing discipline has ever been applied — HIGH

`research_experiments` contains **0 rows**. The Harvey & Liu machinery `CLAUDE.md` mandates
(`research/significance.py`) has never been used. The `ensemble` vote threshold change 0.25 → 0.30 has
no recorded trial count. The 10 stored 2025 `ensemble` runs span +$91.83 to +$156.38 — ten different
configurations, none scored against a best-of-N hurdle.

### 2.7 Live order placement has never executed — MEDIUM

`paper=True` is hardcoded in `bot.py:680` and `dashboard/server.py:47`, deliberately overriding
`ALPACA_PAPER=false`. This is a good guardrail, but it means the live path is untested; going live both
removes a safety net and exercises new code.

---

## 3. Bear markets — answered, not fixed

`docs/bear-market-defence.md` (2026-08-24) settled this properly. Summary of the state:

Latest per-year `ensemble` backtest ($1,000 account, so per §2.5 read the drawdowns as understated):

| Year | Trades | Win% | P&L | PF | Max DD |
|---|---:|---:|---:|---:|---:|
| 2020 | 280 | 86.8% | +$429.47 | 1.75 | 14.0% |
| **2022** | 244 | 74.2% | **−$168.85 (−16.9%)** | **0.80** | **22.0%** |
| 2024 | 310 | 82.6% | +$270.10 | 1.34 | 9.4% |
| 2025 | 256 | 81.3% | +$101.80 | 1.17 | 11.8% |
| 2026 | 108 | 87.0% | +$293.94 | 2.49 | 4.6% |

**What the research established:**

- 17 filter variants across 3 independent mechanisms (market gate, vol-targeted sizing, wider stops) —
  **all failed**. The pre-registered SPY drawdown gate was refuted outright: 2022 got *worse*
  (−$754 vs −$667) while 2023 collapsed from +$2,728 to +$628.
- Cause: 100% of the 2022 loss is stop-outs. The bear *rally* months (Mar/Jul/Nov) were the year's most
  profitable, and every drawdown gate is switched on straight through them.
- The answer is **portfolio selection, not a filter**: `breakout + tqqq_momentum` had zero losing years
  across 2016–2026 (8.7%/yr, worst year +1.4%); `ensemble + tqqq_momentum` cuts the worst year from
  −16.9% to −5.5% for ~60% of the return.
- Honest caveat from the doc itself: **N=1**. 2022 is the only real bear in range; 2018 was profitable.
  The refutation is robust; the recommendation is a survivor selected from 8 candidates on 11 years.

**What has not happened:**

- `market_regime.py` has **zero references in `bot.py`** — left as default-off research tooling, correctly.
- The recommended allocation was never adopted. The bot runs plain `ensemble`, the max-return /
  worst-bear option.
- **It is not currently runnable.** `bot.py:2224` accepts a single `--strategy`; `scripts/manage.ps1`
  passes one `-Strategy`; the singleton rule forbids a second bot process. Multi-strategy allocation
  requires an execution change that does not exist yet.
- None of the three follow-ups in §7 of that doc were run: signal exits on `ensemble`, stops ×1.5–2.0,
  walk-forward validation of the two "never lose" portfolios.

---

## 4. Plan

Ordered by dependency. Phases 0–2 are prerequisites for *any* meaningful go-live conversation, because
until they land there is no trustworthy measurement to base one on.

### Phase 0 — Make measurement trustworthy

Nothing else is worth doing first: today the numbers cannot support a decision.

- [ ] **0.1 — Fix duplicate trade recording.** Find the root cause of 22 rows for one NVDA entry
      (likely repeated `save_trade` on re-entry into the reconcile path, or per-fill recording).
      Add a DB uniqueness constraint on `client_order_id`. Archive existing duplicates to a
      `trades_quarantine` table rather than deleting — they may be real duplicate *orders*, which is a
      separate and worse bug worth confirming against Alpaca's order history.
- [ ] **0.2 — Virtual capital allocation (the shared-account substitute for a sub-account).**
      Add `bot_capital_allocation` to `config.py`. Size against
      `min(allocation × position_size_pct, actual_available_cash)` instead of account-wide equity.
      Real cash must still gate the order — another project may have spent it — but this bot's
      *footprint* becomes bounded and deterministic regardless of what the other 8 projects do.
      Set it so `5 × 20% = allocation`, not 100% of the shared account.
- [ ] **0.3 — Re-base the ledger on the allocation.** Retire the stale
      `starting_capital = $154,114.89`. Report return as a percentage of the allocation. Keep
      `broker_equity` beside it as context only, clearly labelled as account-wide and not this bot's.
- [ ] **0.4 — Quarantine external interference from performance stats.** Exclude
      `external_liquidation` trades from win rate / PF / P&L headline figures and surface them as a
      separate interference counter. An outside flatten is not a strategy outcome. Alert when one occurs.

**Exit criteria:** dashboard win rate, PF and P&L are computed only from this bot's own,
non-interfered, de-duplicated trades, against a fixed allocation.

### Phase 1 — Close the risk holes

- [ ] **1.1 — Earnings filter on all bracket strategies, `ensemble` first.** Wire
      `add_earnings_filter` into the ensemble entry path and block entries within
      `earnings_avoid_days` (3) of a print. Directly addresses the −12.31% META gap, the single largest
      loss on record.
- [ ] **1.2 — Decide the gap-risk posture explicitly.** A stop cannot cap a gap; only position size can.
      Options: reduce `position_size_pct` for any ticker with earnings inside the max-hold window, or
      accept and document the tail. Write down which, and why.
- [ ] **1.3 — Bound the breakeven-gated time stop in time.** AMZN was held 36 days into a −9.86% loss
      against a 6-day max hold. Either add an absolute hold ceiling that overrides the breakeven gate,
      or document the unbounded hold as intentional. Backtest both — this changes the win-rate profile
      substantially and may reduce the headline win rate while improving the distribution.

### Phase 2 — Make the backtest match reality

- [ ] **2.1 — Add `--capital` to the backtest runners.** Default stays $1,000 for continuity with the
      existing corpus; the allocation figure becomes the number decisions are made on.
- [ ] **2.2 — Re-run 2020 / 2022 / 2024 / 2025 / 2026 at the real allocation** for all 8 strategies.
      Expect materially worse drawdowns than the stored rows (§2.5). Treat the existing table as
      historical, not current.
- [ ] **2.3 — Re-validate anything tuned on $1,000 data**, starting with the `ensemble` threshold
      (0.30). Log every run through `db_mod.log_experiment(...)` with an honest `trials` count.
      Per `CLAUDE.md`: expect most of it to fail re-validation. That is the correct outcome.

### Phase 3 — Bear posture, chosen deliberately

- [ ] **3.1 — Make the allocation decision a written one.** Three options from
      `bear-market-defence.md` §7: keep `ensemble` and accept a −17% bear year; move to
      `ensemble + tqqq_momentum` (−5.5% worst, ~60% of return); or `breakout + tqqq_momentum`
      (never lost a year, ~35% of the return). This is a risk-appetite decision, not a technical one.
- [ ] **3.2 — If a combination is chosen, build multi-strategy execution.** `bot.py` needs
      `--strategy a,b`; `manage.ps1` needs to pass it; the singleton rule must hold (one process running
      N strategies, never two processes). Capital splits across strategies *within* the Phase 0
      allocation — and note `bear-market-defence.md` §6 warns combination results will be **worse than
      published** once whole-share fragmentation is modelled, which Phase 2.1 now lets us measure.
- [ ] **3.3 — Run the three untested ideas** from that doc's §7, highest value first: signal/EMA-break
      exits on `ensemble`; stops ×1.5–2.0 as a pure return experiment; walk-forward validation of the
      two "never lose" portfolios.

### Phase 4 — Earn the track record

- [ ] **4.1 — Clean paper run** on the post-Phase-2 configuration, with go/no-go metrics fixed
      **in advance** — minimum trade count, maximum acceptable drawdown, minimum PF — so the decision
      cannot be rationalised after the fact.
- [ ] **4.2 — Require a drawdown in the sample.** The current record covers 2.5 months of a strong tech
      tape and 70% of its profit is three trades. A track record with no adverse period tests nothing.
- [ ] **4.3 — Only then** revisit `paper=True` (`bot.py:680`, `server.py:47`), and treat removing it as
      its own reviewed change with a hard position-size cap far below the paper configuration.

### Explicitly deferred

- **Separate Alpaca account.** Operator decision, 2026-09-10. Phase 0.2–0.4 is the mitigation: it buys
  bounded footprint and clean attribution without a new account. It does **not** prevent another project
  from flattening this bot's positions (§2.1) — that risk remains open and accepted for now, and is the
  main reason a live account would eventually still want isolation.

---

## 5. Go-live gate

Ready means all of:

1. Win rate, PF and P&L computed from de-duplicated, own-only, non-interfered trades — **Phase 0**
2. Bot footprint bounded by an allocation, not by the shared account balance — **Phase 0.2**
3. No stop breach outside a documented, size-mitigated gap scenario — **Phase 1**
4. Parameters validated at the capital actually being traded, with trial counts logged — **Phase 2**
5. Bear posture chosen in writing and, if a combination, actually executable — **Phase 3**
6. A paper track record including an adverse period, scored against pre-registered criteria — **Phase 4**

Currently: **0 of 6.**
