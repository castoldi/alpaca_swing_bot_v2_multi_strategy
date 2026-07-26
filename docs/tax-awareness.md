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

Active only from **1 December** (`tax_guard_start_month` / `_day`). Within the
window it blocks a new entry into a ticker that realised a loss in the trailing
30 days, so no fresh wash sale can leave a replacement open across 31 December.
Outside December it never fires, so 11 months of trading are unchanged.

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
