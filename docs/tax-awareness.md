# Tax Awareness — Wash Sales, Forecasting, and the Year-End Guard

**Added:** 2026-07-26 · **Bot version:** 0.15.0

> **Not tax advice.** This documents mechanical implementations of published IRS
> rules. Basis tracking, corporate actions, and "substantially identical"
> judgements all have edge cases a CPA should review before these numbers are
> relied on for a real filing.

---

## The correction that shaped this design

The original request assumed the wash-sale rule was a calendar window around
**Dec 20 – Jan 10**. It is not.

IRC §1091 applies a **61-day window centred on each individual loss sale**: the
30 days *before* the sale, the sale day, and the 30 days *after*. It applies
**year-round to every sale at a loss**, and the backward-looking half is real —
buying shortly *before* selling at a loss disallows it just the same
([Fidelity](https://www.fidelity.com/learning-center/personal-finance/wash-sales-rules-tax),
[TurboTax](https://turbotax.intuit.com/tax-tips/investments-and-taxes/wash-sale-rule-what-is-it-how-does-it-work-and-more/c5ANd7xnJ)).

The December/January intuition is nevertheless *directionally right*, for a
different reason: it comes from year-end tax-loss harvesting, where a loss taken
in late December has its 30-day replacement window running into January. A loss
realised on 31 December cannot be repurchased until **31 January**.

The correct "flat window" for an active trader turns out to be close to the
original guess: staying clear of a security for **31 consecutive days including
31 December** — roughly **16 December to 15 January** — lets a trader "generally
ignore the wash sale rule with relative impunity"
([TradeLog](https://tradelog.com/education/wash-sales-for-traders/),
[Fairmark](https://fairmark.com/investment-taxation/capital-gain/wash/traders/)).
The instinct was sound; the mechanism and the dates needed fixing.

## The measurement that decided the policy

Run against this bot's own 35 closed live trades:

| Metric | Value |
|---|---|
| Losing trades with a same-ticker purchase inside the 61-day window | **7 of 7 (100%)** |
| Median holding period | **0.9 days** |
| Trades qualifying for long-term treatment | **0** |

Every losing trade this bot has ever made is already a wash sale. That is not a
risk to be avoided — it is the direct consequence of a 5-ticker universe with
sub-day holds and constant re-entry. A year-round 31-day blackout after each loss
would idle most of the universe most of the time.

## Why that turns out not to matter (except once a year)

**A wash sale defers a loss; it does not destroy it.** The disallowed amount is
added to the replacement lot's cost basis and comes back on the next sale. For a
bot that trades continuously and is flat at year end, intra-year wash sales
therefore largely cancel out.

The one case that genuinely costs money is a wash sale whose **replacement
position is still open on 31 December** — that moves the deduction into the
following tax year. That, and only that, is what the guard prevents.

## What was implemented

### 1. Tax ledger (`tax.py`, `tax_records` table)

One record per closed trade, keyed to `trades.id` and therefore to the Alpaca
order IDs already stored there:

`cost_basis` · `proceeds` · `realized_pnl` · `holding_days` · `term`
(short/long) · `is_wash_sale` · `disallowed_loss` · `basis_adjustment` ·
`replacement_trade_id` · `deductible_pnl` · `straddles_year_end`

Records are **recomputed over the whole history**, never patched per trade,
because a later purchase can retroactively wash an earlier loss. `bot.py` calls
`_refresh_tax_records()` after each reconciliation pass; the dashboard also
rebuilds on request.

### 2. Year-end guard (`tax.year_end_entry_block`)

Active from **1 December** (`tax_guard_start_month` / `_day`) **and across the
year boundary for as long as a prior-year loss is still inside its 30-day
replacement window**. Within either window it blocks a new entry into a ticker
that realised a loss in the trailing 30 days. The rest of the year it never
fires, so ~11 months of trading are unchanged.

The January half is not optional, and getting it wrong was a real bug (fixed in
0.15.1): a loss realised **20 December** is still washed by a repurchase on
**10 January** — 21 days later, inside the window. Buying then disallows a
deduction already counted against the prior year and rolls it forward. **Safe
re-entry is 31 days after the loss sale**, which for a late-December loss falls
in the following January.

The guard **fails open**: if the trade history cannot be read it logs and allows
the entry, because deferring a deduction is an optimisation, not a safety rule.
Disable entirely with `tax_year_end_guard = False` — that breaks no rule, it only
forfeits the deferral.

### 3. Dashboard page (`/tax`)

Net capital gain, estimated liability, short/long-term split, gross realized,
wash-sale count and deferred total, year-end straddle watchlist, per-ticker
breakdown, and every closed trade with its wash-sale linkage. Refreshes every
60 seconds and on each trade close.

Rates come from `config.py` (`tax_short_term_rate` 0.24, `tax_long_term_rate`
0.15) and are **assumptions, not your bracket** — set them to your own.

### 4. Lot ledger (`build_lot_ledger`)

Every entry opens a `Lot`; every exit consumes lots under `tax_lot_method` —
**FIFO** (the IRS default absent specific identification), LIFO, or `specific`
with an explicit lot ordering. Sales spanning several lots are split correctly,
and `apply_wash_basis_adjustments` rolls each disallowed loss into the
replacement lot's basis so `adjusted_cost_per_share` reflects it.

Until now, one trade was assumed to be one lot. That happened to hold because
the bot opens one whole-share position per ticker and exits it in full — but
partial fills already occur (`trade_exit_fills`) and would have silently
corrupted basis. This makes it explicit rather than incidental.

### 5. §475(f) mark-to-market switch (`tax_mtm_475f`, default **off**)

When elected: no loss is ever disallowed, the entry guard becomes a no-op, every
position is marked `ordinary`, and the $3,000 capital-loss limit stops binding.
Off by default because the election is irrevocable without IRS consent, requires
Trader Tax Status, and is the account owner's decision with a CPA.

### 6. Substantially-identical groups (`tax_identical_groups`, default empty)

Wash matching is **exact symbol** unless a group says otherwise. Nothing is
inferred: two funds tracking one index are not automatically identical, and
TQQQ-vs-QQQ is unsettled. Populate the config to assert a view; the bot will not.

### 7. Crypto (`tax_crypto_symbols`, default empty)

§1091 reaches "stocks or securities". Digital assets are property, so listed
symbols have gains and losses tracked in full but are never washed. Legislation
extending §1091 to digital assets has been proposed repeatedly — revisit.

### 8. Brackets, NIIT and estimated payments

`tax_use_brackets` switches the forecast from two flat rates to progressive
ordinary brackets, LTCG breakpoints, and the 3.8% NIIT on the lesser of net
investment income and MAGI above $200k single / $250k married-joint. Off by
default so existing figures do not move. `tax_estimated_payments` adds a
four-instalment safe-harbour schedule, the fourth falling in January.

> The bracket **thresholds** in `tax.py` are illustrative and must be verified
> against the IRS revenue procedure for the filing year. The rates and the NIIT
> thresholds are stable; the bracket boundaries move annually.

### 9. Conservative hard block (`tax_hard_block`, default off)

The surgical guard blocks only a ticker with a recent loss. The hard block
refuses **all** entries for a flat window centred on 31 December —
`tax_hard_block_days` (31) runs roughly 16 Dec to 15 Jan. Being wholly out of the
market for 31 consecutive days spanning year end is the standard way an active
trader sidesteps §1091 rather than merely tracking it.

## Measured cost of the guard

Guard on vs off, cached bars, 2024–2026, three-year totals:

| Strategy | Gross OFF | Gross ON | Delta | Entries blocked |
|---|---:|---:|---:|---:|
| ensemble | $611.09 | $607.11 | −$3.98 | 174 |
| regime | $596.50 | $644.54 | **+$48.04** | 143 |
| trend_pullback | $280.83 | $279.09 | −$1.74 | 63 |
| breakout | $155.88 | $141.97 | −$13.91 | 19 |
| momentum_macd | $126.70 | $119.12 | −$7.57 | 2 |
| mean_reversion | $4.54 | $4.54 | $0.00 | 0 |
| tqqq_momentum | $243.78 | $248.62 | +$4.84 | 1 |

The guard is close to free: the deltas are mixed in sign and net slightly
positive overall, which is noise rather than edge. It is not a P&L improvement —
it is a tax-timing control that costs approximately nothing.

## A correction worth recording

The first implementation booked each disallowed loss but never credited it to
the replacement lot's basis — the "defers" half of the rule without the "comes
back" half. Taxable income was overstated by the disallowed total, badly enough
that backtests reported **tax exceeding profit** (mean_reversion: $4.54 gross,
$44.66 tax). Corrected in 0.16.0; the same backtest now shows $8.42. Two tests
had encoded the wrong behaviour and were rewritten around a conservation
property: deferrals move income between lots, they never create or destroy it.

## Facts this surfaces that are worth knowing

- **100% of gains are short-term**, taxed at ordinary income rates. With a 3–7
  day max hold this is structural, not a tuning choice. There is no path to
  long-term treatment without fundamentally changing the strategy.
- **This is a paper account, so none of it is real yet.** Every figure is a
  forecast of what the same activity would produce live.
- **The $3,000 annual capital-loss deduction limit** is modelled: a net loss
  beyond it becomes `loss_carryforward`.

## Two things deliberately left alone

**Cross-account wash sales.** The rule applies across *all* of a taxpayer's
accounts. Another bot shares this Alpaca key trading SPY/QQQ. Whether TQQQ and
QQQ are "substantially identical" is genuinely unsettled — leveraged ETFs on the
same index are usually argued not to be, but the bot should not be asserting a
position on that question. It tracks only its own trades and does not attempt a
cross-bot determination.

**Section 475(f) mark-to-market election.** For a trader who qualifies for Trader
Tax Status, electing MTM **exempts trading entirely from the wash-sale rule** and
removes the capital-loss limitation, converting gains and losses to ordinary
income ([Green Trader Tax](https://greentradertax.com/trader-tax-center/trader-tax-status/section-475-mtm-accounting/),
[Schwab](https://www.schwab.com/learn/story/mark-to-market-trader-taxes)). For a
bot with this trade frequency it is very likely the correct structural answer and
would make the guard redundant. It is also irrevocable without IRS consent, has a
strict filing deadline, and depends on qualifying for TTS — all decisions for the
account owner and a CPA, not for the bot. Flagged here rather than implemented.

## Sources

- [Wash-Sale Rules — Fidelity](https://www.fidelity.com/learning-center/personal-finance/wash-sales-rules-tax)
- [Wash Sale Rule — TurboTax](https://turbotax.intuit.com/tax-tips/investments-and-taxes/wash-sale-rule-what-is-it-how-does-it-work-and-more/c5ANd7xnJ)
- [Wash Sales for Traders — TradeLog](https://tradelog.com/education/wash-sales-for-traders/)
- [Traders and Wash Sales — Fairmark](https://fairmark.com/investment-taxation/capital-gain/wash/traders/)
- [Year-End Tax Trading: Wash Sales and More — Schwab](https://www.schwab.com/learn/story/year-end-tax-trading-wash-sales-and-more)
- [Section 475 MTM Accounting — Green Trader Tax](https://greentradertax.com/trader-tax-center/trader-tax-status/section-475-mtm-accounting/)
- [Trader Status & 475 Mark-to-Market — Schwab](https://www.schwab.com/learn/story/mark-to-market-trader-taxes)
- [For your year-end tax planning, beware the wash sale rule — J.P. Morgan](https://privatebank.jpmorgan.com/nam/en/insights/wealth-planning/for-your-year-end-tax-planning-beware-the-wash-sale-rule)
