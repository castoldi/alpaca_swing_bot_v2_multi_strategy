# Changelog

All notable changes to **Alpaca Swing Bot V2** are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/); versioning is
semantic (`MAJOR.MINOR.PATCH`).

**Versioning model:**
- The canonical semantic version lives in the [`VERSION`](VERSION) file and is bumped
  manually (use `pwsh scripts/version.ps1 -Bump patch|minor|major`).
- Every commit is auto-tagged by the `post-commit` git hook as
  `v<version>+build<N>-<YYYYMMDD-HHMMSS>`, where `<N>` is the total commit count
  (auto-incrementing build number) and the datetime is the commit time.
- List the build history any time with `pwsh scripts/version.ps1 -Builds`.

## [Unreleased]

_Changes landed but not yet released under a new version number go here._

### Added
- **Self-contained P&L ledger ([`portfolio.py`](portfolio.py))** — the bot now
  computes its own balance, realized/unrealized P&L, and lifetime totals from
  the local `trades` table alone. The Alpaca key is shared with several other
  projects, so account equity was never this bot's performance; nothing in the
  ledger reads it.
  - Capital base is `peak_deployed_capital` — the most capital the bot ever held
    at risk at once, derived from local history (currently **$110,703.69**), so
    a percentage return needs no configuration and no segregated account.
  - Intent rows that never became positions (`entry_not_filled`,
    `entry_not_submitted`, zero shares) no longer count as trades: closed-trade
    count and win rate now read 49 / 73.5% instead of 50 / 72.0%.
  - A position with no usable price mark contributes **nothing** to unrealized
    P&L and flips `marks_complete` false, so equity reads as a floor rather than
    a guess.
- **Broker confirmation ([`broker_sync.py`](broker_sync.py))** — read-only sweep
  that verifies only trades carrying this bot's `swingv2-` correlation id
  against Alpaca positions, per trade: `confirmed` / `mismatch` / `missing` /
  `unverified`. Sibling bots' positions are never inspected or claimed. An
  unreadable broker reports `unverified`, never `missing`, so an API outage
  cannot be mistaken for a liquidation. Verdicts persist to
  `trades.broker_status` / `broker_shares` / `broker_checked_at`.
- **`balance_history` table** — one equity-curve point per bot loop, written
  after reconciliation. Stores dollars only; percentages are derived on read
  because the capital base is a running maximum that restates old percentages.
- **`python bot.py --pnl`** — prints the lifetime P&L report from the local
  ledger. Needs no `--strategy`, places no orders, registers no PID.
- **`python bot.py --rebuild-balance-history`** — backfills the daily curve from
  closed-trade history (realized-only; historical marks were never stored).
  Rebuilt rows are tagged `source='rebuilt'` and are the only rows a later
  rebuild may replace, so live snapshots are never destroyed.
- **`GET /api/pnl`** and **`GET /api/balance-history`** — the bot's own numbers,
  as distinct from `/api/account`, which reports the whole shared Alpaca account.

### Fixed
- **Tests no longer write to the live trading database.** Tests driving
  `run_once` stub db functions one at a time, so any call they miss landed in
  `dashboard/swing_bot_v2.db`. A full suite run wrote 17 bogus balance snapshots
  (carrying fake account equity) into the real equity curve; those rows were
  removed. `tests/conftest.py` now redirects `db._DB` to a per-test temp file for
  the whole suite, so no test can reach production regardless of what it stubs.
- `db.get_trades_for_ledger()` reads the full trade history unbounded. Lifetime
  totals previously would have silently truncated at `get_all_trades`' 200-row
  limit once the bot passed that many trades.

### Added
- **[docs/systematic-strategies.md](docs/systematic-strategies.md)** — research
  survey of systematic-trading techniques and how each maps onto this codebase.
  Documents-only; no behaviour change.

  Includes a measured diagnostic of the trading universe from the cached daily
  bars: mean pairwise correlation across NVDA/AMZN/META/AMD is **0.50** over
  2016–2026, rising to **0.66** in the top-quintile-volatility regime and
  **0.86** during the Feb–Apr 2020 crash. Five equal positions therefore behave
  as ~1.7 independent bets normally and ~1.1 in a crash, so the count-based
  `max_concurrent_positions` limit overstates diversification for the core
  universe the same way it did for leveraged ETFs before 0.14.0.

  Priority conclusion: the largest gap is **validation methodology**, not signal
  coverage. The "keep if both backtest years improve" loop in `program.md` has no
  held-out period and does not record the number of configurations tried, so
  selection bias cannot currently be estimated.
- **[docs/markov-and-garch.md](docs/markov-and-garch.md)** — deep dive on Markov
  regime-switching (HMM) and GARCH, with both models implemented from scratch in
  scipy and tested walk-forward on the cached 2016–2026 daily bars. No new
  dependencies; docs-only, no behaviour change.

  Three measured findings:
  - **GARCH is not worth building.** GARCH(1,1) beats the ATR the bot currently
    computes, decisively (pooled Diebold-Mariano −7.84, p < 0.0001), but does
    **not** significantly beat a three-line EWMA (p = 0.12). EWMA captures ~96%
    of the available improvement. Recommendation is EWMA for sizing, keeping ATR
    for TP/SL geometry where a range measure is correct.
  - **An HMM entry gate lost to a 200-day SMA filter.** The 2-state model
    separates regimes cleanly (calm: +0.30%/day at 22% vol; turbulent: −0.01%/day
    at 51% vol, both ~95% persistent), but as an honest walk-forward filter it
    scored Sharpe 0.98 vs 1.20 for the SMA filter on the bot's universe. It did
    cut max drawdown the most (−24.7% vs −60.8% buy-and-hold).
  - **The HMM lookahead trap is worth 2.4×–4.4× fabricated Sharpe.** Scoring the
    identical model with smoothed instead of filtered state probabilities lifted
    Sharpe from 0.98 to 2.38 (bot universe) and 0.56 to 2.45 (S&P proxy).

  Supersedes the ATR-based estimator originally proposed in
  systematic-strategies.md §3.1; that section now carries a pointer.
- **[docs/bear-markets-and-crashes.md](docs/bear-markets-and-crashes.md)** —
  catalog of every S&P 500 bear market (≥20% peak-to-trough) and fast crash since
  1990, using Yahoo Finance daily history to reach 2000–2002 and 2007–2009, which
  the Alpaca cache cannot. Docs-only; no behaviour change.

  Four real ≥20% bears found (2000-03, 2007-10, 2020-02, 2022-01); this bot's own
  tickers fell 1.2×–3.5× harder than the S&P in each one. Point-in-time signal
  tests show bear markets ARE identifiable but only as fast confirmation, not early
  warning — the best signal tested (drawdown ≥10% off the 52-week high) fired
  8–50 days after the top, by which point 23–54% of the eventual decline had
  already happened. Full-history simulation: that signal would have retained 64%
  of buy-and-hold's total return while cutting max drawdown 54% (−26.1% vs
  −56.8%), beating the previously-endorsed 200-day SMA filter on every axis.
  VIX≥30 rejected as a standalone gate — its episodes flicker (avg 8 days) and
  barely reduced drawdown. Recommends an A/B backtest of the two surviving
  signals on the bot's own strategies (not just the index) before implementing a
  market-wide entry gate.













## [0.20.0] - 2026-08-19

### Added

### Fixed

### Changed

## [0.19.2] - 2026-07-29

### Added

### Fixed
- **`_foreign_liquidation_fill` now fails closed on unreadable broker state.**
  Gap in 0.19.1: a bracket's child legs carry *broker-generated* client ids, not
  our `swingv2` prefix, so they are ruled out as "ours" only by being reachable
  through the parent entry/protect order. If that parent lookup failed
  transiently, a perfectly normal stop-loss fill matched every foreign-sell
  criterion and would have been closed as `external_liquidation` — correct exit
  price and P&L, but a wrong reason and a false "another project liquidated you"
  alert. The post-mortem now requires that at least one of our own orders was
  readable before attributing anything to a foreign sell; otherwise the trade is
  left open, matching how `_protective_orders_missing` and
  `_open_leveraged_notional` already fail closed.

### Changed

## [0.19.1] - 2026-07-29

### Added

### Fixed
- **Trades liquidated by a sibling project no longer stay open forever.**
  Follow-up to 0.19.0, which diagnosed the cause but left the damaged rows and
  the recurrence path in place. When another project on the shared Alpaca key
  runs an account-wide `close_all_positions()`, this bot's shares are sold by an
  order it never placed. `_confirmed_exit_fill` correctly refuses to call a
  foreign sell our own exit, so those rows stayed `status='open'` indefinitely,
  logged `"position missing but no confirmed exit fill"` every loop, and each
  silently consumed one of the five position slots. Trades 36–39 (ARM, AMZN,
  NVDA, AMD) had eaten 4 of 5 slots, leaving the bot able to open exactly one
  new position.

  `_reconcile_closed` now runs a last-resort post-mortem via
  `_foreign_liquidation_fill`: if the position is gone and no owned order
  explains it, a *filled* foreign sell of at least our quantity, timestamped
  after our entry and unclaimed by any other trade, closes the row with the new
  `external_liquidation` exit reason and emails an alert. Ownership rules are
  unchanged everywhere else — the bot still never *initiates* an exit on a
  position it cannot prove it owns — and P&L is credited for our share count
  only, so an aggregated flatten covering several bots cannot inflate it.
  Dashboard renders the reason as "⚠ Ext. liquidation".

  Backfilled the four stuck rows from the broker record: −$63.33 realized
  (ARM −$52.56, AMZN −$3.16, NVDA −$4.85, AMD −$2.76). Slots are free again.

  Note the observed timing contradicts the "EOD flatten" description in 0.19.0:
  the liquidations landed **2–58 seconds after each entry** (NVDA bought
  16:28:39, sold 16:28:41), not at the close. Whatever ran it was flattening
  continuously, so stopping a bot at EOD is not sufficient mitigation.

  This is containment, not a cure. Nothing in this repo can stop another process
  on the same key from flattening the account; the durable fix remains a
  separate Alpaca account/key per bot.

### Changed

## [0.19.0] - 2026-07-28

### Added
- **Re-armable broker protection, correlated by order id.** `trades` gains
  `protect_client_order_id` / `protect_alpaca_order_id`, plus
  `db.set_protect_order_ids()`. `bot._place_protective_oco()` re-arms a position
  whose bracket legs died without filling, submitting a **single OCO**
  (TP + SL) under our own `swingv2-protect-<strategy>-<ticker>-<hex>` client id —
  OCO rather than two sells because Alpaca rejects a second concurrent sell leg
  (403 40310000). `bot._protective_orders_missing()` reports whether an open
  position currently has any resting sell we own, and **fails closed**: an
  unreadable order book reads as "protected" so a duplicate can never be stacked
  onto a live leg.

  Why this is needed: the entry bracket's legs are *children of the entry order*,
  so `_confirmed_exit_fill` reaches them via `order.legs`. Legs re-armed later have
  no such parent, so without a stored id their eventual fill is unattributable and
  reconciliation correctly refuses to claim it. `_confirmed_exit_fill` now checks
  `_protect_order_candidates()` **first** (most precise link, no heuristic), and
  `_exit_reason_for_fill` reports `protective_bracket_filled` for those fills —
  matching the OCO parent id too, since the child leg carries a broker-generated
  client id rather than ours.

### Fixed
- **META (trade 40) was holding 33 shares with no broker-side protection.**
  Verified against the account on 2026-07-28: the entry filled, then its stop was
  canceled and its take-profit expired without either filling, leaving the position
  naked while the DB still showed SL $546.93 / TP $635.61. Re-armed via the new
  path (`swingv2-protect-ensemble-META-de9dad54`, OCO TP $635.61 / SL $546.93) and
  confirmed both legs live.

  Root cause is **not** in this repo: nine sibling projects under
  `C:\Data\ai_projects` share one Alpaca paper API key, and
  `day-trader-v2_alpaca_VWAP_Overshoot` / `day-trader-volume-profile-v1` run an
  account-wide `cancel_all_orders()` + `close_all_positions()` EOD flatten, which
  cancels every bracket and liquidates every position in the account regardless of
  which bot opened it. That is what left trades 36–39 stuck at `status='open'` with
  their positions market-sold by orders this bot never placed. Those two bots are
  now stopped; `day-trader-v3` is unaffected — it shares the key but deliberately
  leaves unknown positions untouched. `_reconcile_and_exit` behaved correctly
  throughout, refusing to attribute a foreign sell to its own trade; **the durable
  fix is a separate Alpaca key per bot**, not looser ownership checks.

### Changed

## [0.18.0] - 2026-07-27

### Added
- **Daily-loss kill switch modeled in backtests.** Mirrors `bot.py`'s live
  check: Alpaca's mark-to-market `account.equity` right now compared against
  yesterday's closing equity, blocking new entries once the drop reaches
  `max_daily_loss_pct` (3%). Unlike the entry slippage guard, the live kill
  switch is a global, strategy-agnostic gate (checked once per cycle before
  any per-ticker logic runs, not conditioned on `has_take_profit`), so this
  applies to all 8 strategies, including `sma_50_cross` and `tqqq_momentum`.

  This was the larger of the two live risk protections flagged as missing
  from the backtest a few sessions ago — it needed mark-to-market equity of
  open positions, which the engine had never tracked (only realized cash +
  closed P&L). `run_annual_portfolio` now accepts `price_frames` (ticker ->
  OHLCV, the caller's own timeframe) and re-derives net liquidation value —
  cash plus every open position's current close — at each entry attempt.

  **Re-evaluated fresh at every entry attempt, not latched for the rest of the
  day.** That matches what the live code actually does, not what its comment
  claimed ("no new entries... for the rest of the day") — the real check has
  no daily-sticky state, so equity recovering back above the threshold later
  the same session un-blocks new entries again immediately. Corrected the
  misleading comment in `config.py` to describe the real behaviour; the
  live code itself is unchanged; this is a documentation fix.

- `_price_asof` / `_mark_to_market_equity` helpers in `backtest_portfolio.py`.
  `apply_kill_switch` defaults to on whenever `price_frames` is supplied, off
  otherwise, matching the tax/slippage guards' opt-in-by-data pattern; raises
  if `apply_kill_switch=True` is forced without frames. `max_daily_loss_pct`
  is overridable per call, defaulting from `PARAMS`.
- `PortfolioResult.kill_switch_blocked_entries` / `.kill_switch_trip_days`.
- Wired into `backtest_2025.run_strategy_year` (shared by 2024/2025/2026),
  passing the same `frames` dict already built for candidate collection —
  `backtest_history.py`'s separate cumulative runner is not wired, consistent
  with the tax and slippage guards also being scoped to the annual scripts
  only.
- 20 tests: `_price_asof`/`_mark_to_market_equity` unit tests, a hand-traced
  multi-day scenario (blocks an intraday breach, allows a same-day recovery,
  confirms the next day's baseline resets to yesterday's close rather than
  staying pinned), confirmation exits are never gated, and default/validation
  behaviour.

### Fixed
- **A real bug found while building this**, independent of the kill switch's
  own logic: the day-boundary lookup originally used
  `bar_day - pd.Timedelta(nanoseconds=1)` to mean "just before midnight" and
  fed it to `pandas.Series.asof`, which casts its argument to the index's own
  stored datetime64 resolution and **raises** if that cast would lose
  precision — exactly what happens subtracting a nanosecond against a
  microsecond- or second-resolution index, which is what `pd.to_datetime` on
  a plain string list commonly produces (confirmed: this repo's own test
  frames are `datetime64[us]`). An overly broad `except (KeyError,
  ValueError): return None` silently swallowed that into "no position value"
  — the hand-traced verification scenario caught this immediately (every
  candidate was admitted, zero blocks, when the scenario should have produced
  one). Rewritten to use boolean-mask lookups (`index < as_of` /
  `index <= as_of`), which have no such restriction regardless of the frame's
  stored resolution.

### Notes
- **Measured impact: negligible, and not for lack of trying to find one.**
  Isolated (kill switch on/off, both sides at production defaults otherwise),
  across all 8 strategies and 2020/2024/2025/2026 (32 strategy-years): the
  switch tripped in exactly **2** of them (`regime`/2024, `sma_50_cross`/2025,
  1 trip-day and 1 blocked entry each), and in **both** cases the blocked
  candidate turned out to be one `whole_share_position_size` would have
  rejected anyway (confirmed by diffing the full trade lists — identical
  either way), so the measured P&L delta is **$0.00 across the entire
  sample**, including 2020, the fastest crash in market history.
  This is a real property of this bot's risk profile, not a measurement
  artifact: whole-share sizing capped at 20% of equity across a maximum of 5
  positions makes a genuine same-day 3% *account-wide* mark-to-market
  drawdown a rare event even in a crash year — no single cached year has
  produced one that actually changed an outcome. The switch's value, if any,
  is in tail scenarios worse than anything in the 2016–2026 cached window
  (a genuine multi-day gap-down, or several highly-correlated positions
  moving together) — see the 0.50/0.66/0.86 correlation figures in
  `docs/systematic-strategies.md` for how real that risk is under stress.
- Production `backtest_2024/2025/2026.py` were re-run; **all three years'
  totals are unchanged** ($840.89 / $824.08 / $671.79), consistent with the
  isolated measurement above.
- Verified with a hand-traced scenario checked against a scratch debug script
  before any assertion was written, and the discovered bug was caught by that
  same verification step before being committed — not found later by a user.

## [0.17.1] - 2026-07-27

### Fixed
- **Pre-existing report-generation crash**, found and flagged (not fixed) in
  0.17.0: `build_report_2025.build_report_2025` sourced every ticker's price
  chart from whichever strategy came first in `per_strategy_details`' *dict
  insertion order*, regardless of whether that strategy traded the ticker at
  all. Once strategies gained per-strategy universes (`tqqq_momentum` scoped to
  TQQQ only, in 0.13.0), the first strategy in iteration order frequently had
  no data for a given ticker, handing `ticker_chart` an empty `pd.DataFrame()`
  with no `close` column and crashing `add_indicators` with `KeyError:
  'close'` before `reports/backtest_20XX.html` could be written. Reproduced
  identically on unmodified `main` via `git stash`, confirming it predates
  both this fix and the slippage-guard work in 0.17.0.

  Now searches every strategy's per-ticker data for the first one that
  actually has it — any strategy that traded a ticker downloaded the same
  OHLCV, so the first match is as good as any. `ticker_chart` also gained a
  defensive fallback (a "no price data available" placeholder) for the
  degenerate case where trades are recorded but no strategy retained a price
  frame, so a similar gap can no longer crash report generation even if the
  root cause recurs elsewhere.

  4 regression tests in `tests/test_build_report_ticker_universe.py`,
  confirmed to fail with the exact original `KeyError: 'close'` when the fix
  is reverted.

### Corrected
- **The 0.17.0 changelog's "combined 2024 total" entry was wrong** — its
  before/after numbers were transposed due to a `git stash`/`stash pop`
  sequence used to check whether the report crash pre-existed. That check was
  run correctly, but I mislabeled which resulting database rows corresponded
  to which state, and reported the swap as if it were the guard's effect.

  Verified now with a clean, unambiguous methodology — both variants run in
  the same process from identical cached data, differing only in the `params`
  object passed to `collect_backtest_candidates`, with no stash involved and
  no reliance on database timestamps:

  | | guard ON (1.5%, production) | guard OFF | delta |
  |---|---:|---:|---:|
  | ensemble, 2024 | $270.10 | $279.75 | **−$9.64** |
  | ensemble, all 4 years | $1,037.99 | $935.72 | **+$102.27** |
  | trend_pullback, all 4 years | $644.04 | $697.32 | −$53.28 |
  | breakout / momentum_macd | unchanged | unchanged | $0.00 |
  | mean_reversion, all 4 years | $91.22 | $83.84 | +$7.39 |
  | regime, all 4 years | $920.56 | $922.21 | −$1.65 |

  This is close to the isolated measurement 0.17.0 already reported correctly
  (ensemble +$104.61 there vs +$102.27 here — the small difference is the tax
  guard's production default now left on for both sides instead of forced off).
  **The direction and rough magnitude in 0.17.0's "Notes" section were right;
  only the single "$840.88 → $831.62" combined-total line was backwards.**

  Confirmed by 5 repeated fresh process invocations (`PYTHONHASHSEED=random`)
  of the exact production code path, all returning the identical $270.10 for
  ensemble/2024 — the current numbers are reproducible and trustworthy. I do
  not have a confirmed root cause for the original transposition; the
  suspicion is a mislabeled comparison on my part while narrating the stash
  sequence, not non-determinism in the engine itself, but I want to be honest
  that I have not proven which.

  Re-ran all three years fresh under the corrected understanding: **2024 =
  $840.89, 2025 = $824.08, 2026 = $671.79** (3-year total $2,336.76). These
  match what 0.17.0 already had for 2025/2026, and 2024 is unchanged from
  0.17.0's own (correctly reported) "before" figure — i.e., today's real,
  verified totals were the true state all along; only the "after" comparison
  value in that one line was wrong.

### Added
- All three static reports (`reports/backtest_2024.html`,
  `backtest_2025.html`, `backtest_2026.html`) now regenerate successfully and
  are current as of this release.

## [0.17.0] - 2026-07-27

### Added
- **Entry slippage guard in backtests**, mirroring the live check in `bot.py`
  exactly: signals whose next-bar open drifts more than
  `entry_max_slippage_pct` (1.5%) from the signal bar's close are skipped, since
  the SL/TP geometry computed off that close no longer matches. Applies only
  where live applies it — bracket strategies (`has_take_profit=True`);
  `signal_with_stop` strategies (`sma_50_cross`, `tqqq_momentum`) already fill
  at the next bar's open with no guard, live and in the backtest, and are
  untouched. The guard is symmetric (drift is checked in absolute value, so a
  favourably cheaper fill invalidates the geometry same as an adverse one), and
  a signal on the last bar of the backtest window has no next bar to price a
  fill from and is skipped, matching the existing boundary rule for
  `signal_with_stop` candidates.

  Previously the backtest filled every bracket entry at the exact signal close
  with zero friction — a "free lunch" the live bot has never had. There was no
  live/backtest divergence report for this until now; it closes the same class
  of gap as the tax guard's backtest wiring in 0.16.0.
- 7 tests in `tests/test_backtest_portfolio.py`: admits within tolerance, skips
  beyond it, symmetric for a downward gap, boundary is inclusive at exactly
  1.5%, a signal on the last window bar is skipped, data past the window
  boundary is never used to price a fill (would be lookahead), and
  `signal_with_stop` strategies are confirmed unaffected by an extreme gap.

### Notes
- **Measured impact, isolated from the tax guard, 2020/2024/2025/2026, six
  bracket strategies:** trend_pullback −$52.47, breakout $0.00, mean_reversion
  +$7.39, momentum_macd $0.00, regime −$0.60, ensemble **+$104.61** — net
  effect across the sample is small and mixed in sign, like the tax guard
  before it. breakout and momentum_macd never triggered it in this sample.
  ensemble's gain is concentrated in 2020, where skipping a few crash-window
  entries whose fill gapped away from the signal price helped more than it
  cost.
- Combined with the tax guard (both apply their production defaults), the
  2024 backtest total moves from $840.88 to $831.62 — a $9.26 net change
  across all 8 strategies. 2025 and 2026 were re-run and refreshed the same
  way; totals are $824.08 and $671.79 respectively.
- `backtest_2024.py` / `backtest_2025.py` / `backtest_2026.py` were re-run so
  `backtest_runs` (and therefore `/api/backtest-results`) reflect the guard.
- **Found, not fixed: a pre-existing report-generation bug**, unrelated to this
  change (reproduced identically on unmodified `main` via `git stash`).
  `build_report_2025.build_report_2025`'s `ticker_chart` crashes with
  `KeyError: 'close'` before writing `reports/backtest_20XX.html`, so those
  three static report pages are stale from 2026-07-18 even though the
  underlying `backtest_runs` rows (and the dashboard's numeric views) are
  current. Left as a known issue — out of scope here.

## [0.16.0] - 2026-07-26

### Fixed
- **Wash-sale deferrals were booked but never recovered, overstating taxable
  income by the disallowed total.** A wash sale defers a loss into the
  replacement lot's basis; the loss was removed from the sale but never added to
  the replacement, so it vanished. Bad enough that backtests reported **tax
  exceeding profit** — `mean_reversion` showed $4.54 gross P&L against $44.66 of
  tax; it is $8.42 now. Reproduced minimally: a −$100 loss washed by a
  replacement later sold for +$50 is a −$50 economic result, and was being
  reported as a **+$50 taxable gain**.

  `_return_deferred_losses` now credits each disallowance to its replacement
  record. When the replacement is still open the deferral correctly carries
  forward instead.

  Two tests had encoded the wrong behaviour and were rewritten around a
  conservation property — deferrals move income between lots, they never create
  or destroy it. `basis_adjustment` was also being used for both "deferred out"
  and "received in", double-counting it; it now means only the latter.

### Added
- **§475(f) mark-to-market switch** (`tax_mtm_475f`, **default off**). Elected:
  no loss is disallowed, the entry guard becomes a no-op, positions are marked
  `ordinary`, and the $3,000 capital-loss limit stops binding. Off by default —
  irrevocable without IRS consent, requires Trader Tax Status, and is the
  owner's decision with a CPA.
- **Configurable substantially-identical groups** (`tax_identical_groups`,
  default empty). Matching stays **exact-symbol** unless a group says otherwise;
  nothing is inferred, and TQQQ-vs-QQQ remains a view the operator must assert.
- **Crypto tracking without §1091** (`tax_crypto_symbols`, default empty).
  Listed symbols have gains and losses tracked in full but are never washed,
  since digital assets are property rather than securities.
- **Conservative hard block** (`tax_hard_block`, default off). Refuses *all*
  entries for a flat window centred on 31 December — `tax_hard_block_days` (31)
  runs ~16 Dec to ~15 Jan — alongside the existing surgical per-ticker guard.
- **FIFO / LIFO / specific-lot ledger** (`build_lot_ledger`, `tax_lot_method`,
  default `fifo`). Every entry opens a lot, every exit consumes lots in the
  configured order, sales spanning multiple lots split correctly, and
  `apply_wash_basis_adjustments` rolls deferrals into `adjusted_cost_per_share`.
  Previously one trade was *assumed* to be one lot; partial fills already occur
  and would have silently corrupted basis.
- **Progressive brackets, NIIT and estimated payments** (`tax_use_brackets`,
  default off; `tax_filing_status`, `tax_other_income`, `tax_niit`,
  `tax_estimated_payments`). Ordinary brackets stack on other income, LTCG
  breakpoints apply above them, NIIT adds 3.8% on the lesser of net investment
  income and MAGI over $200k single / $250k married-joint, and a four-instalment
  safe-harbour schedule is produced with Q4 falling in January.
- **Tax in backtests.** `run_annual_portfolio` now simulates the year-end guard
  (evaluated only against trades realized *before* each entry, never the full
  history) and reports `tax_estimate`, `after_tax_pnl`, `wash_sale_count`,
  `disallowed_loss` and `tax_blocked_entries`. This closes a live/backtest
  divergence: the guard changed live trading in 0.15.0 without ever being
  measured.
- `/api/tax` and the `/tax` page now expose open lots with wash-adjusted basis,
  the estimated-payment schedule, and the active election/guard configuration.
- 33 tests in `tests/test_tax_advanced.py`, plus 3 in `tests/test_tax.py` for
  the deferral-conservation regression.

### Notes
- **Measured cost of the year-end guard**, on vs off, 2024–2026 three-year
  totals: ensemble −$3.98, regime **+$48.04**, trend_pullback −$1.74, breakout
  −$13.91, momentum_macd −$7.57, mean_reversion $0.00, tqqq_momentum +$4.84.
  Mixed in sign and net slightly positive — the guard is close to free, and is a
  tax-timing control rather than a P&L improvement.
- Every new switch defaults to neutral, so no figure moves without opting in.
  The one intended change is the deferral fix above: the current database now
  reports $64.95 net capital gain and $15.59 tax, versus $66.53 / $15.97 before.
- **Bracket thresholds in `tax.py` are illustrative** and must be verified
  against the IRS revenue procedure for the filing year. Rates and the NIIT
  thresholds are stable; the bracket boundaries move annually.
- Still not implemented: cross-account matching against the other bot on the
  shared Alpaca key.
- Not tax advice; have a CPA review before relying on these numbers.

## [0.15.1] - 2026-07-26

### Fixed
- **The year-end wash-sale guard switched itself off in January — exactly when
  a December loss is most exposed.** `year_end_entry_block` compared `now`
  against 1 December *of the current year*, so on 10 January that test became
  "is 10 Jan 2027 after 1 Dec 2027", which is false, and the guard returned
  "allow".

  The result was the precise case the guard exists to prevent: a loss realised
  20 December followed by a repurchase on 10 January is 21 days later, inside
  the 30-day replacement window, so it **is** a wash sale — and the bot would
  have taken it, pulling a deduction out of the prior tax year and rolling it
  forward.

  The guard now stays armed across the year boundary. A trailing-30-day loss
  booked in an earlier tax year is always guard-relevant, independent of the
  December window, because re-entering disallows a deduction already counted
  against that year.

  Verified against the reported scenario (20 Dec loss):

  | Re-entry attempt | Days after loss | Before | After |
  |---|---|---|---|
  | 28 Dec | +8 | blocked | blocked |
  | 10 Jan | +21 | **allowed** | **blocked** |
  | 19 Jan | +30 | **allowed** | **blocked** |
  | 20 Jan | +31 | allowed | allowed |

  Safe re-entry is 31 days after the loss sale, which for a late-December loss
  lands in the following January.

### Added
- 7 tests in `tests/test_tax.py` covering the January tail: a parametrised
  boundary sweep across the year change, the block message naming which tax
  year the deduction would leave, and confirmation that a prior-year *gain*
  does not trigger the guard.

## [0.15.0] - 2026-07-26

### Added
- **Tax awareness: wash-sale tracking, per-trade tax records, and a `/tax`
  dashboard page.** Full write-up in
  [docs/tax-awareness.md](docs/tax-awareness.md).

  **The premise was corrected first.** The wash-sale rule (IRC §1091) is not a
  calendar window around 20 Dec – 10 Jan. It is a **61-day window centred on
  each loss sale** — 30 days before, the sale day, 30 days after — applying
  year-round, and the backward-looking half counts too.

  **Measured on this bot's own 35 closed live trades: 7 of 7 losing trades
  (100%) are already wash sales**, median hold 0.9 days, zero trades eligible
  for long-term treatment. That follows directly from a 5-ticker universe with
  sub-day holds, so year-round wash-sale avoidance would idle most of the
  universe.

  **A wash sale defers a loss rather than destroying it** — the disallowed
  amount joins the replacement lot's basis and returns on the next sale — so
  intra-year the effect largely cancels. The only case that truly costs is a
  wash sale whose replacement is still open on 31 December, which moves the
  deduction into the next tax year. That is what the guard targets.

- `tax.py` — pure, dependency-free engine: 61-day wash-sale detection in both
  directions, proportional disallowance for partial replacements, short/long
  term classification, year-end straddle detection, and an annual forecast that
  models the $3,000 capital-loss deduction limit and cross-bucket netting.
- `tax_records` table keyed to `trades.id` (and therefore to the Alpaca order
  IDs already stored there): cost basis, proceeds, realized P&L, holding days,
  term, wash-sale flag, disallowed loss, basis adjustment, replacement trade
  link, deductible P&L, year-end straddle flag. Records are **recomputed over
  the whole history** on every refresh, never patched per trade, because a later
  purchase can retroactively wash an earlier loss.
- `bot._tax_entry_block()` — year-end guard, active only from 1 December
  (`tax_guard_start_month` / `_day`), blocking re-entry into a ticker that
  realised a loss in the trailing 30 days. **Fails open**: an unreadable history
  logs and allows the trade, since deferring a deduction is an optimisation, not
  a safety rule. Disable with `tax_year_end_guard = False`.
- `bot._refresh_tax_records()` after each reconciliation pass.
- `/tax` page and `/api/tax`: net capital gain, estimated liability, short/long
  split, wash-sale count and deferred total, year-end straddle watchlist,
  per-ticker breakdown, and every closed trade with its wash-sale linkage.
- `tax_short_term_rate` (0.24) and `tax_long_term_rate` (0.15) in
  `StrategyParams` — forecast assumptions only, they change no trading
  behaviour.
- 30 tests in `tests/test_tax.py`.

### Notes
- Against the current database: 34 tax records, 7 wash sales, $1.58 of deferred
  loss, $66.53 short-term net, ~$15.97 estimated liability, and **zero**
  straddling year end.
- **This is a paper account, so no real tax liability exists.** Every figure is
  a forecast of what the same activity would produce live.
- **100% of gains are short-term**, taxed at ordinary income rates. With a 3–7
  day max hold that is structural, not a tuning choice.
- Deliberately not implemented: cross-account wash-sale matching against the
  other bot on the shared key (whether TQQQ and QQQ are "substantially
  identical" is unsettled, and the bot should not assert a position), and the
  **§475(f) mark-to-market election**, which would exempt trading from the
  wash-sale rule entirely and is very likely the correct structural answer at
  this trade frequency — but is irrevocable without IRS consent and depends on
  qualifying for Trader Tax Status. Both are flagged in the doc.
- Not tax advice; have a CPA review before relying on these numbers.

## [0.14.1] - 2026-07-25

### Fixed
- **Another bot sharing the Alpaca key no longer consumes this bot's position
  slots.** `_load_live_sizing` counted *every* open account position against
  `max_concurrent_positions`, so a second bot day-trading SPY/QQQ on the same
  key silently reduced this bot's capacity — and at five foreign positions
  would have stopped it entering anything at all while still logging a healthy
  cycle and reporting "5-position account limit reached".

  Slot usage is now scoped to positions this bot actually owns, matched against
  the tickers in its own open-trade records.

  **Deliberately left account-wide**, because they are genuinely shared:
  - `equity` and `cash` — another bot spending cash really does reduce what
    this one can deploy.
  - `_open_leveraged_notional` — unchanged from 0.14.0, where counting
    positions "whoever opened them" is the documented intent: the leveraged cap
    is about account risk, not order ownership.
  - the daily-loss kill switch, which reads account equity and therefore
    couples the two bots' drawdowns in both directions.

  The scoping **fails closed**: when open trades cannot be read, every position
  is charged to this bot, costing capacity rather than risking over-allocation.

### Added
- `bot._bot_owned_symbols()` and `bot._our_open_position_count()`.
- `tests/test_bot_shared_account.py` — 10 tests covering both the shared-key
  slot accounting and the duplicate-entry guards that had no coverage:
  - foreign positions do not consume slots; our own still do; an account full
    of foreign positions still leaves five slots free
  - unreadable ownership falls back to the conservative full count
  - cash and equity stay account-wide
  - an open DB trade blocks a second entry — the guard whose upstream failure
    produced 21 real NVDA orders for one signal (live trades 14–34)
  - an untracked broker position blocks stacking
  - a non-404 position-lookup failure fails closed
  - control: a clear ticker still enters

### Notes
- Related operational risk, not addressed here: if the shared key is ever used
  on a **live** account under $25k, the other bot's day trades can trigger
  pattern-day-trader restrictions that block *this* bot's exits, stranding
  positions past their stops. Paper-only today, so latent.

## [0.14.0] - 2026-07-19

### Added
- **Leveraged-ETF exposure cap** (`max_leveraged_exposure_pct`, default 0.20).
  Total notional across `LEVERAGED_TICKERS` is capped as a fraction of equity,
  enforced identically in live sizing and in `run_annual_portfolio`.

  The account position limit is count-based — `max_concurrent_positions` (5) x
  `position_size_pct` (20%) = 100% of equity — so it cannot see correlation.
  Leveraged ETFs track correlated underlyings and their entry signals fire
  together, so a multi-ETF leveraged universe could put the entire account into
  3x instruments simultaneously: roughly 3x account beta, cash-funded, with no
  margin call to stop it. This closes that hole before the universe can grow.

  The 0.20 default equals exactly one 20% position — the exposure a
  single-leveraged-ticker universe already has — so **behaviour is unchanged
  today** (2024/2025/2026 `tqqq_momentum` backtests are byte-identical), and
  adding a second leveraged ticker cannot raise risk until the cap is
  deliberately raised.
- `position_sizing.leveraged_headroom()` and an optional `max_notional`
  ceiling on `whole_share_position_size()`, for group-level limits the
  per-position fraction cannot express.
- `LiveSizingState.leveraged_notional`, read from real broker positions
  (whoever opened them — the cap is about account risk, not order ownership)
  and reserved within a cycle as entries are placed.
- 19 tests in `tests/test_leveraged_exposure_cap.py`.

### Notes
- `_open_leveraged_notional()` **fails closed**: a position whose market value
  cannot be read is charged the full cap, blocking further leveraged entries
  rather than silently freeing headroom.
- Research (not shipped): the strategy was run unmodified on 8 3x ETFs over
  2016-2026 — all 8 profitable, median PF 1.66, median max DD 5.8%, 77% of
  years positive. TECL (+$582) and SOXL (+$537) both beat TQQQ (+$523), so the
  rule is not curve-fit to TQQQ. It degrades on choppy underlyings (TNA small
  caps PF 1.06). TQQQ's -$2.58 worst year is the family outlier; a typical
  worst year is -$25 to -$50.


## [0.13.0] - 2026-07-19

### Added
- **`tqqq_momentum` strategy (8th strategy)** — TSI(25,13,13) crossing its
  signal line enters; a 4h close below EMA(50) exits; 8% broker-held emergency
  stop; no take-profit. Scoped to TQQQ only. Backtested on Alpaca SIP 4h bars:

  | year | TQQQ buy & hold | strategy (20% sizing, $1k account) |
  |------|-----------------|-------------------------------------|
  | 2024 | +65.5%          | +$37.49, PF 1.55, DD 4.6% |
  | 2025 | +36.8%          | +$177.75, PF 5.66, DD 1.9% |
  | 2026 | +27.8%          | +$27.78, PF 2.53, DD 1.3% |

  At full allocation over 2022–2026 it returned +309.6% with a 23% max
  drawdown, against buy-and-hold's +65.8% with an 81% drawdown — and was
  positive in 2022, when TQQQ itself fell 79.5%.
- **`LEVERAGED_TICKERS` / `ALL_TICKERS` in `config.py`** — leveraged ETFs are
  deliberately kept out of the shared `TICKERS` universe.
- **Per-strategy ticker scoping** via `BaseStrategy.tickers` and the
  `strategy_universe()` helper. A strategy that declares `tickers` trades only
  those; every other strategy keeps trading the shared universe and can never
  reach a leveraged ETF. Backtests download only the union of the universes
  actually being run.
- **`tsi()` indicator** in `strategies/base.py`; `add_indicators` now also
  emits `tsi`, `tsi_signal`, and `ema_trend`.
- 15 tests in `tests/test_tqqq_momentum.py` covering the entry/exit rules, the
  stop wiring, and the scoping guarantees in both directions.

### Changed
- `BaseStrategy.stop_loss_fraction()` lets a `signal_with_stop` strategy set its
  own emergency stop; the backtest engine no longer hardcodes
  `sma_cross_stop_loss_pct` for every signal-exit strategy.
- `BaseStrategy.signal_exit_reason` replaces the hardcoded `"sma_cross_down"`
  in `bot._exit_reason_for_fill`, so a signal exit is now labelled per strategy
  (`ema_break` for `tqqq_momentum`). Report and dashboard exit summaries count
  both reasons; the exit pie chart's slice is now "Signal Exit".

### Notes
- The bot runs **one strategy per process**, so trading this live means starting
  the bot with `-Strategy tqqq_momentum` *instead of* the current strategy —
  it does not run alongside `ensemble`.
- An entry filter using EMA(50) or MACD was tested and rejected: an ablation
  showed both *reduced* returns versus TSI alone (TSI-only +309.6%, +EMA50 gate
  +161.4%, +MACD +157.3% over 2022–2026). A 3xATR take-profit scored higher
  in-sample (+346%) but was a jagged parameter spike (3.5xATR fell to +144%),
  so it was left out as an overfit artifact.


## [0.12.0] - 2026-07-18

Safety hardening pass after a full code review: every finding was verified
empirically against the Alpaca paper API and by 2024–2026 backtest comparison.

### Changed
- **Single protected bracket for every entry quantity.** The 3-leg scaled TP
  ladder + stepped-stop engine was removed entirely: an empirical probe showed
  Alpaca rejects the extra sell legs while the entry buy is open
  (`403 40310000: cannot open a short sell while a long buy order is open`),
  so the scaled path could never execute as coded — and had it been accepted,
  stop-outs would have orphaned GTC sells able to short the margin account.
  A 2024–2026 backtest comparison also showed the single bracket outperforms
  the scale-out at account scale (+$261k vs +$182k total across strategies at
  $100k equity). `materialize_candidate` now models the single-exit path so
  backtests match live behavior.
- **Trading window follows Alpaca's market clock** (`get_clock().is_open`)
  instead of a fixed 08:30–17:00 ET window: no more premarket-queued market
  orders, and holidays/early closes are handled. Conservative 09:30–16:00 ET
  weekday fallback if the clock API is unavailable.
- **Live signals only evaluate completed candles**: `completed_bars` now drops
  the still-forming 4h bucket (it previously only trimmed the daily session),
  removing intra-bar entries the backtest could never see.
- **Reconciliation covers all strategies' open trades**, not just the running
  strategy's, so restarting the bot with a different `-Strategy` no longer
  orphans older positions (daily exit frames are fetched on demand).

### Added
- **Daily-loss kill switch**: when equity is down ≥3% (configurable
  `max_daily_loss_pct`) vs yesterday's close, new entries are disabled for the
  rest of the day (one alert email per day); exits and broker-held protection
  keep running.
- **Entry slippage guard** (`entry_max_slippage_pct`, 1.5%): entries are
  skipped when the live price has drifted too far from the signal bar close,
  keeping the SL/TP geometry consistent with the backtest.
- **Real fill price recording**: the broker's average entry fill (and filled
  quantity) is persisted per trade (`entry_filled_price`) and preferred over
  the signal close for P&L, breakeven checks, and the time stop.

### Fixed
- Pre-entry position check treated *any* API error as "no position"; now only
  a definitive 404 allows the entry and all other failures fail closed.
- Stepped-stop bugs (counting TP fills from previous trades on the same
  ticker; permanently losing the stop after a failed cancel/replace) are gone
  with the machinery — protection is now a single broker-held OCO that cannot
  desynchronize.

## [0.11.0] - 2026-07-18

### Added
- Added a shared whole-share sizing policy and chronological annual portfolio
  ledger with cash, ticker, and five-position capacity controls.
- Added test-first design and implementation documentation for live
  20%-of-equity sizing and annual-reset backtest compounding.

### Fixed
- Scale-out legs now consume one backtest position slot instead of being
  treated as independent concurrent trades.
- Live cycles now count existing Alpaca positions and reserve capacity after
  each submitted entry, preventing whole-share rounding from admitting a
  sixth account position.
- Live entries now persist a durable client-id intent before broker submission,
  attach the broker id after acceptance, adopt timeout-ambiguous submissions by
  client id, and retain unresolved intents until Alpaca confirms absence.
- Reconciliation retires an aged pending entry only after Alpaca explicitly
  confirms that its client id is absent, preventing phantom or orphaned orders
  across submission failures and process restarts.
- Each live cycle resolves durable intents before sizing and disables new
  entries while any earlier parent order is active or unverifiable, so pending
  cash and position capacity cannot be reused after a restart or strategy swap.
- Scaled entries now use an atomic stop-only OTO parent; cash/slot capacity is
  reserved before separate profit targets are submitted, and partial target
  setup failures require confirmed cleanup while the position remains tracked
  and broker-protected.
- Simultaneous backtest entries now share the same pre-event realized equity,
  preventing same-bar exits from leaking future P&L into another ticker's
  opening quantity.
- Multi-year report curves are labeled as independent-year P&L aggregates
  instead of implying that capital compounds across calendar years.

### Changed
- Live entries now use protected whole-share orders capped at 20% of current
  Alpaca equity and available cash, with local cash reservation preventing
  multiple signals in one cycle from relying on margin.
- Annual and historical backtests now compound realized P&L within each year,
  model the live one/two-share versus scaled exit paths, and reset every
  calendar year to a fresh $1,000.
- Dashboard and reports now expose percentage sizing, starting/ending annual
  equity, returns, and the five-position maximum.

## [0.10.0] - 2026-07-18

### Added
- Added a transactional SQLite cache for Alpaca SIP bars with incremental
  prefix/suffix refreshes, IPO-aware empty-range coverage, and idempotent
  OHLCV upserts.
- Added a cumulative 2016–present historical backtest runner with HTML and JSON
  outputs, yearly strategy summaries, cached data coverage metadata, and
  optional date/strategy selection.
- Added the approved design and test-first implementation plan for persistent
  historical market data and the range backtest workflow.

### Fixed
- Historical download failures now propagate in strict mode instead of being
  recorded as successfully cached empty data.

### Changed
- Historical backtests now use consolidated Alpaca SIP data through the local
  cache, while live bot market data remains on IEX.
- Long-running bot, dashboard, backtest, and pytest processes now use separate
  rotating log files to avoid Windows file-handler conflicts.

## [0.9.0] - 2026-07-18

### Added
- Design and implementation specifications for the daily **SMA 50 Cross** strategy, including empirical comparison of long-only, stop-protected, long/short, and existing-risk-overlay variants. The selected design is long-only with a broker-held 10% emergency stop and a close-on-cross-below exit.
- Registered the `sma_50_cross` strategy with exact completed-daily-close entry/exit cross rules, a 50-day SMA, and a 10% emergency-stop parameter.
- Added strategy-specific Alpaca bar fetching for `4h` and `1d`, with a guard that removes the still-forming current daily candle from live signal evaluation.
- Added a dedicated next-session backtest lifecycle for signal-exit strategies: enter at the next open, prioritize the 10% stop (including gap-through fills), and exit at the next open after a daily cross below.
- Live SMA 50 Cross entries now use Alpaca stop-only OTO orders sized from a fresh snapshot; cross-down exits reuse the bot's ownership proof and cancel only the attached stop before closing the bot-owned quantity.
- Annual backtests now cache bars by ticker and strategy timeframe, record the SMA strategy as `1d`, and render its no-target trades, cross exits, parameters, and color correctly in Plotly reports.
- Dashboard strategy examples and cards now respect per-strategy timeframes, label SMA exits, and omit take-profit visuals for strategies without a target; README, agent guidance, and research logs document the seventh strategy and its evaluation.

### Fixed
- Dashboard Home metadata now reports both configured timeframes (`4h + 1d`) instead of implying that the new daily strategy also runs on 4-hour candles.
- Dashboard and generated reports now derive the strategy count from the registry/results instead of retaining the old hardcoded count of six.
- Current-year daily backtests now discard the still-forming session candle before evaluating SMA crosses.
- Live crossover exits now persist durable intent before changing protection, fail closed unless Alpaca confirms the attached OTO stop is canceled, refresh the remaining position quantity after cancellation, and remain pending until broker fills are confirmed. Partial stop/market fills are accumulated idempotently for correct weighted exit P&L, while restarts and failed submissions resume the exit even after the one-bar cross condition has passed.

### Changed
- Added `.worktrees/` to `.gitignore` so isolated feature checkouts cannot be staged as project content.

## [0.8.3] - 2026-07-07

### Fixed
- **Single-share entries (qty<3, the common case at $200/trade) were never getting broker-side stop-loss/take-profit protection.** The bracket order request omitted `order_class=OrderClass.BRACKET`, so Alpaca silently accepted it as a plain market order and dropped the `take_profit`/`stop_loss` legs entirely — confirmed via order history that zero LIMIT/STOP orders were ever placed for these entries. Now sets `order_class=OrderClass.BRACKET` explicitly.
- Order-status comparisons across `bot.py` (`_status_str`) were checking `str(order.status)` against plain values like `"filled"`, but alpaca-py's `OrderStatus` renders as `"OrderStatus.FILLED"` via `Enum.__str__`, so the comparison silently never matched anything. This broke TP-leg counting, stepped-stop sync, and exit-fill reconciliation across the board. Fixed by comparing `.value` instead.
- Reconciliation now recognizes when an entry order never filled and was canceled/expired/rejected by the broker (0 shares filled) — it closes the DB trade as `entry_not_filled` instead of logging the same "position missing" warning on every loop forever.
- `_entry_order_candidates` now requests `nested=True` when fetching the entry order by id, so bracket child legs (TP/SL) are actually returned — previously always empty, so exit-fill matching for single-share entries never worked via the intended path.
- Exit-fill matching now refuses to attribute the same broker fill to more than one DB trade (`db.exit_order_already_used`), closing the remaining gap behind the 0.8.2 duplicate-entry fix.

## [0.8.2] - 2026-07-04

### Added

### Fixed
- Bot no longer treats weekends or exchange holidays as tradable just because the time is between 08:30 and 17:00 ET; the loop now checks the Alpaca market calendar before running.
- Live reconciliation no longer closes a newly opened trade by matching an unrelated historical sell fill. Missing-position reconciliation now requires a confirmed filled exit leg tied to the trade's entry order, preventing repeated NVDA entry emails/orders from one persistent signal.

### Changed

## [0.8.1] - 2026-06-24

### Added
- **Active Strategies card** on the dashboard Home tab: a row of pills showing every strategy, which are enabled (config `ENABLED_STRATEGIES`) vs. disabled, with the one the live bot is currently looping marked **● RUNNING**.

### Fixed
- `manage.ps1` `Stop-Dashboard` could return "stopped" while the old `pythonw.exe` uvicorn process was still alive, so the follow-up start probed it, saw it healthy, and refused to replace it — leaving stale code serving. Now:
  - process sweeps (`Get-DashboardProcesses` / `Get-BotProcesses`) match **both `python.exe` and `pythonw.exe`** (the script launches with `pythonw.exe`);
  - `Stop-Tree` verifies the kill and falls back to `Stop-Process -Force`;
  - `Stop-Dashboard` waits for the port to actually free (and re-kills the holder) before reporting success.

### Changed

## [0.8.0] - 2026-06-24

### Added
- **Dashboard live bot status hero**: RUNNING / HUNG / STOPPED indicator (pulsing dot) with strategy, loop interval, last-loop age, uptime, PID, and whether the bot is inside its 08:30–17:00 ET trading window. Backed by new `/api/bot-status`.
- **Live account panel**: Alpaca equity, day P&L ($ and %), buying power, and cash via new `/api/account`.
- **Live Universe board**: per-ticker last price + day change (%) for the watched symbols, with a "held" badge when the bot has an open position. Includes a real Alpaca market-clock pill (open/closed + next open/close). Backed by new `/api/market` (+ `data_feed.fetch_snapshots`).
- **Bot Orders @ Alpaca table**: only orders the bot placed (filtered by the `swingv2` client-order-id prefix), colour-coded by leg (entry/TP/stop/exit) with fill price and status — proof of opens/closes straight from the broker. Backed by new `/api/bot-orders`.
- **Open Positions** now render live from Alpaca with unrealized P&L ($ and %) instead of DB entry levels.
- `runtime.read_status()`: single-source bot health readout (mirrors the `manage.ps1` / `keep_alive.py` heartbeat-freshness formula).

### Changed
- Home dashboard refreshes live status/quotes every 15s (backtest tables stay on 30s).

## [0.7.0] - 2026-06-21

### Added
- `keep_alive.py`: windowless watchdog (pythonw) that checks bot + dashboard health every 30 min and restarts via `manage.ps1` if either is down. Healthy = no action. Logs to `logs/keepalive.log`.
- `scripts/setup_keepalive_task.ps1`: one-shot Admin script to register `AlpacaSwingBotKeepAlive` Windows Scheduled Task (every 30 min, `IgnoreNew`, 10-min execution limit).
- `docs/keepalive.md`: AI instruction doc for the watchdog system.
- `requirements.txt`: added `psutil>=5.9.0` (used by keep_alive.py).

### Fixed

### Changed

## [0.6.0] - 2026-06-18

### Added
- **`strategies/` package** — each strategy is now its own file with a `BaseStrategy` ABC interface (`strategies/base.py`). New files: `trend_pullback.py`, `breakout.py`, `mean_reversion.py`, `momentum_macd.py`, `regime_adaptive.py`, `ensemble.py`.
- **Strategy registry** (`strategies/__init__.py`) — `REGISTRY` dict maps name → instance; `get_enabled()` / `get_all()` / `get_strategy(name)` helpers. `strategy.py` is now a backwards-compat shim re-exporting everything.
- **`ENABLED_STRATEGIES` in `config.py`** — remove a strategy name from this set to disable it in both the bot and backtests without touching any other code.
- **`GET /api/strategies`** — returns all registered strategies with metadata (label, version, color, description, params_display, enabled) + latest backtest P&L per year.
- **`--strategy` flag for all three backtest scripts** — `python backtest_2025.py --strategy breakout` runs and logs only that strategy.

### Changed
- Dashboard Strategies tab is now driven by `/api/strategies` (no longer hardcoded JS). Disabled strategies render with a banner and reduced opacity.
- All backtest scripts iterate `get_enabled()` instead of a hardcoded `StrategyType` list.
- `bot.py` uses `REGISTRY[strat_name].check_entry(...)` instead of `get_entry_checker`.

## [0.5.1] - 2026-06-18

### Changed
- Bot loop now enforces trading hours: `run_once` is skipped outside 08:30–17:00 ET; loop continues heartbeating so manage.ps1 health checks pass

## [0.5.0] - 2026-06-14

### Added
- **3-level take-profit scale-out + stepped stop** (design:
  `docs/superpowers/specs/2026-06-14-trailing-stop-3tp-design.md`, plan:
  `docs/superpowers/plans/2026-06-14-trailing-stop-3tp.md`).
  - TP1/TP2/TP3 at 1/3, 2/3, and full of each strategy's existing ATR target;
    position split 33/33/34. `config.TP_SPLITS`; `strategy.split_take_profit` /
    `split_qty`; `EntrySignal` exposes `tp1/tp2/tp3`.
  - **Stepped stop** (not a continuous trail): initial SL → **breakeven after TP1**
    → **TP1 after TP2**. Time-stop applies to the remainder.
  - Backtest: new `strategy.simulate_exit_scaleout` (conservative intrabar priority —
    stop checked first; floor raised effective next bar). `backtest_ticker` emits one
    `Trade` row per leg (`tp1`/`tp2`/`tp3`/`stop_loss`/`time_stop`/`end_of_data`);
    `compute_stats` counts `tp1/2/3` as take-profits.
  - Live: `_place_scaled_entry` (market entry + 3 GTC limit legs + a full-qty stop),
    and `_sync_stepped_stop` which ratchets the resting stop each loop from live
    Alpaca order/position state. Entry now branches: `qty>=3` scales out, `qty 1-2`
    falls back to a single OCO bracket at TP3, `qty<1` skips. Reachability checks TP1.
  - Dashboard labels `tp1/tp2/tp3` exit reasons.
- **pytest test suite** under `tests/` (helpers + scale-out simulation + backtest +
  stats + live order helpers, using a fake Alpaca client).
- All three 4h backtests rerun with scale-out and recorded as history.

### Changed
- ⚠️ **Backtested P&L dropped materially under scale-out on the current universe.**
  Taking 1/3 off at the near target and ratcheting the stop to breakeven/TP1 caps the
  big trending winners the single-TP model rode to the full target. Several strategies
  flipped negative for 2026 (e.g. ensemble +$213→−$198, regime +$242→−$139;
  momentum_macd held up best). The feature is correct per spec — but on these momentum
  names the single-TP exit performed better. Worth tuning (back-loaded splits, or a
  less aggressive breakeven move) before relying on it.

### Fixed
- `post-commit` auto-push forced non-interactive (`GIT_TERMINAL_PROMPT=0`,
  `GCM_INTERACTIVE=never`) so it can never hang on a credential prompt headlessly.

## [0.4.0] - 2026-06-13

### Added
- **4h candle timeframe across the whole system** (was daily). New `data_feed.py`
  sources 4h bars from **Alpaca** (`StockHistoricalDataClient`, IEX feed) — yfinance
  has no native 4h interval and caps intraday history at ~730 days (2024 unavailable),
  so Alpaca is used for all years. Centralised in `config.BAR_TIMEFRAME = "4h"`.
- **Historical backtest records.** `backtest_runs` gains a `timeframe` column
  (existing rows tagged `1d`); every rerun is **kept** as history rather than
  overwritten. New `get_backtest_history()` + `GET /api/backtest-history`, and a
  **Backtest History** table on the dashboard Home tab showing every run
  (timestamp, timeframe, stats). The headline tables now show the *latest* run per
  strategy/year via `get_backtest_results()`, with a timeframe badge.
- All three 4h backtests (2024/2025/2026) rerun and recorded.
- **Auto-push**: the `post-commit` hook now pushes the commit + tag to upstream
  (best-effort, non-fatal) so every commit is committed, tagged, and pushed.

### Changed
- Backtests (`download_history`), the live bot (`fetch_bars`), and the Strategies-page
  charts (`strategy_examples.py`) all fetch 4h bars via `data_feed`. The live bot now
  trades on 4h signals; its time-stop converts the bar-based max-hold to calendar days.
- Indicator/holding params are unchanged (literal timeframe switch) — on 4h bars they
  now span a shorter calendar window (e.g. SMA-50 ≈ 25 trading days).
- `CLAUDE.md` / `AGENTS.md`: documented the mandatory version + changelog +
  commit/push/tag-with-datetime release workflow.

## [0.3.0] - 2026-06-13

### Added
- **Per-strategy candlestick examples on the Strategies page.** Each strategy card
  now shows **2 annotated candlestick charts** of real, recent setups so the strategy
  is easy to visualise: entry marker, dotted **SL** and **TP** lines, the exit marker,
  and a title with the outcome (e.g. `ARM · +12% · take profit (1d)`).
- `dashboard/strategy_examples.py` — generates the examples by reusing the strategies'
  own `get_entry_checker` + `simulate_exit` over ~18 months of daily bars, preferring
  the most recent **resolved** trades (real SL/TP/time-stop outcomes) with ticker
  variety. Cached in-process and on disk (`reports/strategy_examples_cache.json`,
  6-hour TTL) since the yfinance fetch + scan takes a few seconds.
- `GET /api/strategy-examples` (threadpool-backed, `?refresh=true` to force a rebuild)
  serving the cached examples; Plotly.js mini-charts rendered client-side in the
  existing dark theme.

## [0.2.0] - 2026-06-13

### Added
- **Alpaca correlation ids on every trade** — each entry order is submitted with a
  unique `client_order_id` (`swingv2-entry-<strategy>-<ticker>-<uuid>`). The DB
  `trades` table now stores `client_order_id` + `alpaca_order_id` for the entry and
  `exit_client_order_id` + `exit_alpaca_order_id` for the exit, so every position is
  traceable back to the exact Alpaca order. Entry qty is recorded in `shares`.
- **Exit reconciliation** — when a bracket SL/TP fills, the bot finds the closing
  fill and records the exit (price, P&L, exit correlation ids) in the DB, so trades
  are tracked all the way through to close instead of being stuck `open`.
- **Bot-scoped time-stop** — positions past their per-strategy `max_holding_days` and
  at breakeven+ are closed by the bot (only after ownership is verified).
- DB helpers `get_open_trades_by_strategy`, `get_open_trade`, and an idempotent
  `_migrate()` that adds the new columns to existing databases.

### Fixed
- **The bot would close positions it did not open.** The old `_check_open_positions`
  called `tc.close_position(ticker)` for *any* symbol in `TICKERS`, liquidating the
  entire position — including shares a human or another strategy opened. It also
  called `close_trade(...)` with a mismatched signature (ticker passed as the row id).

### Changed
- **Closing is now strictly bot-owned and partial-safe.** The bot only inspects
  trades it recorded, verifies ownership via the entry order's `client_order_id`
  (failing **closed** — if ownership can't be proven, the position is left alone),
  cancels only its own bracket legs, and sells only the quantity it opened (any
  non-bot shares of the same symbol are left untouched).
- Entry de-duplication now keys off this bot's own open DB trade, and the bot will
  not stack onto a pre-existing untracked position.

## [0.1.0] - 2026-06-13

First versioned release. Establishes the email/duplicate fixes and the
build-version + auto-tag workflow.

### Added
- **Versioning & build tags** — `VERSION` file, this `CHANGELOG.md`, a `post-commit`
  git hook (`scripts/git-hooks/post-commit`) that tags every commit
  `v<version>+build<N>-<datetime>`, and `scripts/version.ps1` to show/bump the
  version and list builds. Hook is installed via `core.hooksPath = scripts/git-hooks`.
- **Singleton process manager** — `scripts/manage.ps1` (`status`, `start-bot`,
  `stop-bot`, `restart-bot`, `start-dashboard`, `stop-dashboard`,
  `restart-dashboard`). Idempotent: refuses to spawn a duplicate when a healthy
  instance is already running; replaces dead/hung ones and sweeps orphans.
- **Runtime PID/heartbeat tracking** — `runtime.py`; the bot writes `run/bot.pid`,
  `run/bot.meta.json`, and a per-loop `run/bot.heartbeat` so health (alive **and**
  looping) can be detected. Dashboard PID/meta written by the manager.

### Fixed
- **Email flood** — two root causes eliminated:
  1. Duplicate `--loop` bots were running simultaneously, each emailing every 30 min.
     The manager now prevents duplicates.
  2. The "Qty 0" bug: with `dollars_per_trade=$200`, stocks priced >$200 (e.g. ARM)
     computed `qty=0`, fell through, and emailed "Qty 0" every loop while opening an
     unprotected position. Such entries are now skipped entirely (no order, no email).

### Changed
- High-priced stocks (`qty < 1`) are skipped instead of placed as bare notional
  orders. Raise `dollars_per_trade` in `config.py` to trade them with proper brackets.
- `CLAUDE.md` / `AGENTS.md` updated with the no-duplicate rule, PID-finding
  instructions, the health model, and the manager-based restart workflow.















