# Design: Stepped trailing stop + 3 take-profit levels

**Date:** 2026-06-14
**Status:** Approved design (pending spec review)
**Scope:** Backtests, live bot, DB, dashboard. All 6 strategies, all trades.

## Goal

Replace today's single all-or-nothing exit (one fixed SL + one TP, full position)
with a **3-level scale-out** and a **stepped (ratcheting) stop** that locks in
progressively more profit as each take-profit fills.

## Exit model

For an entry at `E` with the strategy's existing (clamped, ATR-based) target `T`:

- **TP1 = E + (T − E)·1/3**, **TP2 = E + (T − E)·2/3**, **TP3 = T**.
- Position split **33% / 33% / 34%** across TP1 / TP2 / TP3.
- **Stepped stop** (no continuous trailing) on the *remaining* shares:
  - Before TP1: stop = initial `SL` (the strategy's existing stop).
  - After TP1 fills: stop → **E (breakeven)**.
  - After TP2 fills: stop → **TP1 price**.
  - After TP3 fills: position fully closed.

```
TP3 = T      ── sell last 34% ──────────────── trade closed
TP2          ── sell 33% ── stop moves to TP1
TP1          ── sell 33% ── stop moves to entry (breakeven)
E   (entry)  ── stop = initial SL until TP1
SL  (initial)◀ protects full position pre-TP1
```

The **time-stop** still applies to whatever shares remain (close remainder after
max-hold bars if at breakeven+). The entry filter `is_tp_reachable_in_days` checks
**TP1** (the nearest target), so entries are not over-rejected.

## Signal model

`EntrySignal` gains `tp1`, `tp2`, `tp3` (floats). `take_profit` is retained as an
alias for `tp3` for backward compatibility. A single central helper
`split_take_profit(entry, take_profit) -> (tp1, tp2, tp3)` computes the thirds, so
the 6 strategy entry-checkers are **not** individually rewritten — each still
computes its clamped `T`, and the helper derives the ladder. Splits/fractions live
in `config` (`TP_SPLITS = (0.33, 0.33, 0.34)`) so they're tunable in one place.

## Backtest (`strategy.py`)

`simulate_exit` is rewritten to walk forward and produce **per-leg exits**:

- Track remaining shares and the current stepped stop floor.
- For each bar, **conservative intrabar priority**: check the **stop first**
  (`low <= stop_floor`) against the *current* floor; if hit, exit all remaining
  shares at the stop. Otherwise check **TP1 → TP2 → TP3** in order (`high >= TPk`);
  a single wide bar may fill multiple TPs. Stepped-floor updates take effect from
  the **next bar** (a TP fill does not retroactively change the same bar's stop).
- Time-stop and end-of-data close the remainder.

`backtest_ticker` emits **one `Trade` row per partial exit**. Each row carries its
slice's `shares`, `exit_price`, `pnl_dollars/pct`, and an `exit_reason` from the
expanded vocabulary: `tp1`, `tp2`, `tp3`, `stop_loss`, `time_stop`, `end_of_data`.
(One entry → up to 4 rows: up to 3 TP legs + a stop/time/eod leg for the remainder.)

`compute_stats` (backtest_2025.py) is updated so `tp1/tp2/tp3` all count as
take-profit exits; win-rate / P&L / profit-factor sum naturally over the per-leg
rows. Trade **counts will rise** vs. the daily/single-TP runs — expected and noted.

Backtests always scale out with **fractional thirds** (shares = dollars/price), so
the ladder is fully exercised regardless of price.

## Live bot (`bot.py`)

Alpaca brackets support only one TP + one SL, so the single bracket is replaced:

1. **Entry:** plain market buy for `qty` shares (bot-owned `client_order_id`).
2. **Exits placed immediately after fill:**
   - 3 **limit sell** orders, ⌊qty/3⌋ each (remainder on TP3), at TP1/TP2/TP3.
   - 1 **stop** (sell-stop) order for the **full** qty at the initial `SL`.
   Every order carries a `swingv2-…` `client_order_id` and is recorded in the DB.
3. **Per loop, `_reconcile_and_exit` manages the stepped stop:** detect filled TP
   legs (order status / position-qty drop); when TP1 has filled, cancel + replace
   the stop at **breakeven**; when TP2 has filled, replace it at **TP1**; reduce
   the stop qty to match remaining shares. All adjustments are ownership-verified
   (same guard as today: act only on bot-owned positions/orders).

**Protection guarantee:** a resting stop for the full remaining qty is *always*
live on Alpaca, so the downside is never unprotected. Only the *tightening* of the
stop (to breakeven / TP1) happens on the next loop after a TP fill — benign lag,
since by then the trade is in profit.

**Sizing fallback:** scale-out needs ≥3 whole shares.
- `qty >= 3`: full 3-leg scale-out as above.
- `qty < 3` (incl. the common `qty < 1` skip): **fall back to today's behavior** —
  a single OCO bracket at `TP3` with the initial stop (or skip if `qty < 1`).

> **Practical note:** with `dollars_per_trade=$200` on the current $200–500 universe,
> live orders are already `qty < 1` and skipped, so live scale-out won't trigger
> until `dollars_per_trade` (and the `$1,000` cap) are raised enough to buy ≥3
> shares. This is out of scope here; backtests demonstrate the feature regardless.

## DB + dashboard

- `dashboard/db.py`: accept the new `exit_reason` values; store the per-leg exit
  correlation ids (reuse existing `exit_client_order_id`/`exit_alpaca_order_id`).
  No schema migration needed beyond what already exists (a trade row per leg).
- Dashboard trades/history tables already key off `exit_reason` and `pnl` — they
  render the new reasons as-is; strategy-card example charts will draw TP1/TP2/TP3
  lines instead of a single TP (nice-to-have, low priority).

## Rollout

1. Implement signal helper + config splits.
2. Rewrite `simulate_exit` + `backtest_ticker` emission; update `compute_stats`.
3. Update live `bot.py` order placement + stepped-stop management.
4. Update DB/dashboard exit-reason handling.
5. **Rerun all three 4h backtests**, verify rows recorded.
6. Version bump (minor → 0.5.0), CHANGELOG, commit (hook tags + pushes).

## Out of scope (YAGNI)

- Continuous price trailing (explicitly chosen: stepped only).
- Re-tuning indicator/TP params for the new exit model.
- Raising `dollars_per_trade` / position-cap changes (user can do later).
- Per-leg partial exits beyond 3 levels; configurable level count.

## Edge cases

- **Gap through multiple TPs in one bar:** fill TP1→TP2(→TP3) in order that bar.
- **Stop and TP in same bar:** stop checked first (conservative) against the
  pre-bar floor.
- **qty not divisible by 3 (live):** ⌊qty/3⌋ per leg, remainder added to TP3 leg.
- **Time-stop / EOD with shares left:** close remainder, tagged `time_stop`/`end_of_data`.
- **Reachability filter:** evaluated against TP1.
