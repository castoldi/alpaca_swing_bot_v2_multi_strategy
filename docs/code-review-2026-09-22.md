# Code review — 2026-09-22

**Code baseline:** `986cb61` (v0.25.1), clean working tree
**Scope:** the live path (`bot.py`, `data_feed.py`, `strategies/`, `dashboard/db.py`,
`dashboard/server.py`, `keep_alive.py`, `notifier.py`) and its parity with
`backtest_execution.py`. This review follows
[code-review-2026-09-05.md](code-review-2026-09-05.md) (F01–F16) and
[code-review-2026-09-21-verification.md](code-review-2026-09-21-verification.md)
(V01–V09). It does not repeat those findings.
**Method:** read the source; checked claims against today's live log
(`logs/alpaca_swing_bot_v2_multi_strategy.log`), the live ledger
(`dashboard/swing_bot_v2.db`, opened read-only) and the installed `alpaca-py`.
Ran the full suite: **841 passed**, one existing `websockets.legacy` warning.
No orders were placed, no services were restarted and no backtests were run.

**Out of scope:** the two known blockers still stand and are not re-argued here.
The Alpaca paper key is shared with 8 other projects (separate account deferred
2026-09-10), and the live-readiness verdict is still **NO**
([live-readiness-2026-09-10.md](live-readiness-2026-09-10.md)).

## Summary

The V01 fix works in production. The two entries since it shipped (trades 77
and 78) filled 3.7 and 4.1 minutes after the modelled fill time. On 2026-09-21,
before the fix, entries filled 15 minutes late, and trade 69 on 2026-09-11 filled
164 minutes late. The reconciler's ownership and idempotency guards are sound.

The remaining problems are narrower than F01–F16, and none of them breaks
protection. One can close a live trade in the ledger on a transient API error.
One sends a false alarm on normal entries at the open. One can freeze the loop for
an hour. Portfolio concentration is a risk-design gap, not a code bug.

| ID | Priority | Finding |
|---|---|---|
| R01 | **P2** | A transient `get_open_position` error is treated as "no position", which can finalize a live trade as `external_liquidation` |
| R02 | P3 | A just-submitted entry that has not filled yet triggers a "reconciliation blocked" email and marks the run as errored (seen live on 2026-09-22) |
| R03 | P3 | Alpaca REST calls have no HTTP timeout; one hung socket stalls the loop until the watchdog restarts it (up to 65 min) |
| R04 | P3 | Five 20% slots across five correlated tech names: the account goes 100% into one factor, often on a single bar |
| R05 | P3 | SQLite connections are never closed, use the default 5 s lock timeout, and re-run schema DDL on every call, on a file that backtests also write |
| R06 | P4 | Signals for tickers already held are logged, stored and counted as signals |
| R07 | P4 | The fixed-sleep loop fills up to interval + 5 min after the backtest's modelled fill minute |
| R08 | P4 | Dashboard `/api/bot-orders` misses bracket exit legs and can drop the bot's own orders |
| R09 | P4 | Comment and doc drift, dead code, and a stale worktree |
| R10 | P4 | The dashboard serves account data on `0.0.0.0` with no authentication |

---

## R01 — Transient position error treated as "no position" (P2)

**Location:** `bot.py:1380-1383` (`_reconcile_and_exit`) and `bot.py:2226-2229`
(`_execute_exit_intent`).

```python
try:
    pos = tc.get_open_position(ticker)
except Exception:
    pos = None  # Alpaca raises when there is no position for the symbol
```

Every exception becomes "the position is gone", including 429, 5xx, timeouts
and DNS failures. With `pos is None`, the trade goes to `_reconcile_closed`,
which ends in `_foreign_liquidation_fill`. That function closes the trade locally
as `external_liquidation` if any non-`swingv2` filled sell of the same symbol,
for at least our quantity, exists after our entry. Other projects on the shared
key trade the same mega-caps, so such a sell is plausible.

**Failure scenario:** we hold 90 AMZN with a live GTC bracket. A sibling bot
sells 100 AMZN from its own position. On the next cycle `get_open_position`
returns a 503. The trade is marked closed with a fabricated P&L at the sibling's
price, and an "AMZN liquidated externally" email goes out. The broker still holds
our shares and bracket. From then on the time stop never runs for that position.
The "untracked position exists" guard blocks new AMZN entries. If the bracket
later fills, that fill is not attributed to any trade.

The entry path already does this correctly (`bot.py:940-951`): only a 404 means
there is no position.

**Fix:** add one helper, `_open_position_or_none(tc, ticker)`, that returns
`None` only on a 404 and re-raises anything else. Use it at both call sites. A
re-raised error becomes a per-trade reconciliation failure and fails closed,
which is the existing, alerted path. `_position_qty` (dead code, see R09) has
the same pattern and should be deleted.

**Tests:** fake `get_open_position` raising a 503, with a qualifying foreign sell
present. The trade must stay open with no `close_trade` call and one failure
recorded. A 404 must still reach `_reconcile_closed`.

---

## R02 — False "reconciliation blocked" alert on a pending entry (P3)

**Location:** `bot.py:2056-2057` (`_reconcile_entry_fill` raises
`"entry quantity is still unsettled"`), caught at `bot.py:1451-1461`, which
records a failure and calls `_alert_once`.

**Evidence** (live log, 2026-09-22, times are local):

```
08:33:21  Bracket entry: AMZN x90 @ ~$255.67 ...
08:33:28  [ERROR] Exit check failed for AMZN: entry quantity is still unsettled
08:33:29  Email sent: Bot V2: AMZN reconciliation blocked
08:33:32  Bot V2 run complete: 4 signals, 1 orders — status=error
```

The market order was submitted three minutes after the open and filled at
13:33:44 UTC, 23 s after submission. `_record_entry_fill` polls for only 2 s.
The end-of-run reconciliation then found a normal working order, raised, and the
email told the operator to "check that the position still has a live stop". The
run is also stored as `error` in `bot_runs`. Any entry that does not fill within
about 7 s will do this. Entries at the open are the most exposed, because every
overnight-bar signal fills then.

**Fix:** raise a distinct `EntryPending` exception when the parent is in an
active state (`new`, `accepted`, `pending_new`, `partially_filled`). In
`_reconcile_and_exit`, log it and skip the trade while the order is younger than
`ENTRY_PENDING_GRACE` (5 min). Past the grace period, keep today's behaviour
(failure plus alert), because a market order unfilled for 5 minutes during the
session is abnormal. `_protection_gap` already returns None in this case.

**Tests:** a pending parent 30 s old gives no failure and no alert. The same
parent 10 minutes old gives one alert. A mismatched-ownership error still alerts
immediately.

---

## R03 — No HTTP timeouts on Alpaca REST calls (P3)

**Location:** `alpaca/common/rest.py:195` in the installed SDK calls
`self._session.request(method, url, **opts)` with no `timeout`. `bot._get_trading`
and `data_feed._get_client` use the defaults.

`requests` without a timeout waits forever on a half-open socket. The loop
heartbeats only before and after `run_once`. A hang therefore goes undetected
until the heartbeat is older than `interval * 2 * 60 + 300` = 3,900 s. The
watchdog then restarts the bot on its next 30-minute tick, so a stall can last
up to about 95 minutes. Broker-held GTC stops still protect the positions, but in
that window no time stop, signal exit or protection repair runs. Every fill
window that falls inside it is also lost.

**Fix:** after building each client, wrap its session so every request gets a
default `timeout=(5, 30)`. Mount an adapter, or wrap `session.request` with
`functools.partial`. Keep alpaca-py's own 429 retry. Also log the duration of each
`run_once` call, so slow cycles are visible before they become stalls.

**Tests:** assert that the wrapped session passes a timeout. A fake session that
raises `requests.Timeout` becomes a per-ticker error and does not stop the run.

---

## R04 — Concentration: 100% of equity in one factor (P3, risk design)

`TICKERS` is NVDA, AMZN, META, AMD and ARM, which are all mega-cap tech or
semiconductor names. With `max_concurrent_positions = 5` and
`position_size_pct = 0.20`, the portfolio's natural full state is "own the whole
universe". The ledger shows this happening:

- **2026-09-21 16:14–16:15 UTC:** NVDA, META, AMD and ARM were all entered on
  the same 12:00 bar within 14 s, about 80% of equity.
- **2026-09-22:** five open trades worth about $115k against $115.9k account
  equity. Account cash was $24k before the last entry.

The count-based slot limit cannot see correlation. The leveraged-ETF cap exists
for this reason, but it covers only `LEVERAGED_TICKERS`. The daily kill switch
(−3%) only blocks new entries. A sector gap of −9% hits every position at once,
and the stops sit at −9%, so one bad morning costs about 9% of the account with
nothing left to stop it. The annual backtests have the same property, so their
drawdowns include it. This is not a live/backtest mismatch; it is how the
strategy is designed.

**Fix (research first, per the research-loop rules):**

1. Add a generic **group exposure cap** that generalizes `max_leveraged_exposure_pct`:
   `exposure_groups = {"semis": ("NVDA","AMD","ARM"), ...}`, each with its own cap,
   enforced in `position_sizing` for live and in `run_annual_portfolio`. Keep the
   default equal to today's behaviour (no cap), so the change is plumbing only.
2. Separately, test caps such as max 2 per group or 40% gross per group over
   2020, 2022 and 2024–2026. Count every cap value tried as a trial in
   `evaluate_search`. Keep a cap only if it clears the hurdle **or** cuts max
   drawdown materially at an acceptable P&L cost. State that trade-off
   explicitly; a risk cap does not need to win on P&L.
3. Longer term, diversify the universe. That is its own research item.

---

## R05 — SQLite connection handling (P3)

**Location:** `dashboard/db.py:16-21` and every `with _con() as c:` (34 calls to
`_ensure_tables()`).

- `sqlite3.Connection.__exit__` commits or rolls back but **does not close**.
  Closing depends on CPython reference counting, and any leaked reference (for
  example a traceback) keeps a file handle open.
- Default `timeout=5`. The live bot, the dashboard and the backtest scripts all
  write `dashboard/swing_bot_v2.db`. A write the bot cannot make within 5 s
  raises. `save_trade` fails closed, so there is no entry. A failed
  `set_signal_cursor` loses the ticker's bar for this cycle, and a failed
  `close_trade` is retried later. All three paths are safe but cost trades or
  delay bookkeeping. `market_cache.py` already uses `timeout=120`.
- `_ensure_tables()` runs a full `executescript` of `CREATE TABLE IF NOT EXISTS`
  statements, plus the migration checks, on every CRUD call. `executescript` also
  commits any open transaction first.

**Fix:** use a `contextlib.contextmanager` that opens with `timeout=30`, commits
or rolls back, and always closes. Run `_ensure_tables()` once per process,
behind a module flag reset by `_DB` changes, since tests monkeypatch `_DB`.
Add `PRAGMA busy_timeout` for clarity.

**Tests:** a writer holding a lock for 2 s does not make `set_signal_cursor`
fail. The connection is closed after each call; check this by patching
`sqlite3.connect`.

---

## R06 — Held-ticker signals counted as signals (P4)

**Location:** `bot.py:905-925`. `trades_found += 1` and `bot_hooks.log_signal`
run before the "already holds an open trade" check. A 09:33 run on 2026-09-22
logged 4 signals and placed 1 order; three of the "signals" were tickers already
held. The `signals` table and the dashboard's signal counts are inflated by this,
and so is any analysis built on them.

**Fix:** move the open-trade check before counting and logging. Alternatively,
store the skip reason on the signal row (`skipped_reason = "held"`), which keeps
the signal visible and lets it be filtered out.

---

## R07 — Fixed-sleep loop versus the modelled fill minute (P4)

**Location:** `bot.py:2575-2590`. The loop sleeps `interval * 60` after each
pass, so passes land at an arbitrary phase. `_signal_is_actionable` accepts a
signal for `interval + 5 min` after its modelled fill time. A live fill is
therefore 0–35 minutes after the minute the backtest fills at. After V01 the
measured delays are 3.7 and 4.1 min (trades 77 and 78). Before V01 they ran up to
164 min (trade 69), and 15 min for all four entries on 2026-09-21. Run time adds
drift (4–15 s per pass today). If a pass ever takes more than 5 min, a signal can
fall between two passes and be skipped.

**Fix:** make passes wake at the next scheduled fill time plus 30 s. For 4h bars
those are 4h bucket boundaries during the session and 09:30 ET; for daily bars,
09:30 ET. Keep the 30-minute cadence for reconciliation in between. Record
`entry_filled_at − modelled fill time` and the price gap on each trade, so live
slippage against the model is measured, not assumed.

---

## R08 — `/api/bot-orders` misses exit legs (P4)

**Location:** `dashboard/server.py:196-237`. The endpoint reads the last 300
orders account-wide with `nested=False`, then keeps those whose client id
starts with `swingv2`. Bracket and OCO children carry broker-generated client
ids, so every stop and TP fill is filtered out. Nine projects share the key, so
the 300 most recent orders can also exclude the bot's older orders entirely.

**Fix:** query with `nested=True` and keep children of owned parents. Better,
build the list from the order ids already in the `trades` table (entry, protect,
exit) and fetch those directly.

---

## R09 — Drift and dead code (P4)

- `config.py`, `max_daily_loss_pct` comment: says "the account is down … vs
  yesterday's close equity". Since V08 the switch uses the bot's own P&L over
  yesterday's equity, and falls back to the account-wide drop.
- `bot.py:915`: the comment says "reachable within ~2 trading days", but the call
  passes `days=4` and the ATR is a **4h-bar** ATR, so the rule is "within 4 bar
  ATRs". CLAUDE.md's "≤4 ATR-days" has the same drift.
- `_position_qty` (`bot.py:490`) and `_days_held` are only used by the stale
  `.worktrees/sma-50-cross` worktree. Its branch `feat/sma-50-cross` is fully
  merged (`git log main..feat/sma-50-cross` is empty). Delete the dead helpers
  and remove the worktree.

---

## R10 — Unauthenticated dashboard on the LAN (P4)

`scripts/run_dashboard.py` binds `0.0.0.0:8004`. Every endpoint is read-only,
but `/api/account`, `/api/positions`, `/api/bot-orders` and `/api/tax` expose
balances, positions and tax estimates to anyone on the network, and
`/api/strategy-examples?refresh=true` lets anyone trigger a heavy data fetch.
Phone access is a requirement, so the fix must keep it working: add a shared
token (a query string or cookie set once on the phone), or put the dashboard on
Tailscale. Low priority on a home network.

---

## Strategy observations (research, not bugs)

These need evidence before any change. Each is a candidate for the research loop
with an honest trial count.

1. **Ensemble in practice behaves like Regime.** Experiment 5 found regime-only
   entries identical to live. The other four members add code, compute and a
   misleading description without changing trades. Either rename it for what it
   does, or re-derive the weights. Re-deriving is a parameter search and must be
   priced as one.
2. **Exit asymmetry.** Across the closed trades, `time_stop` accounts for 28
   trades and +$14.7k, while `bracket_filled` (TP and SL together) accounts for 39
   trades and +$2.4k. The breakeven-gated time stop lets losers run to the −9%
   stop. The trend-break exit already recommended in
   [bear-market-playbook.md](bear-market-playbook.md) is the natural test; run it
   together with R04, not before.

---

## Plan

Each phase is one release: CHANGELOG entry, version bump, commit and push. After
any change to `bot.py`, restart with `manage.ps1 restart-bot` and confirm both
services are HEALTHY.

### Phase 1 — Live safety and alert hygiene · v0.25.2 (patch)

| Step | Work | Done when |
|---|---|---|
| 1 | R01: add `_open_position_or_none` (404-only), use it at both call sites, delete `_position_qty` | New 503 and 404 tests pass; no other `except Exception: pos = None` remains in `bot.py` |
| 2 | R02: add `EntryPending` with a grace window in the reconciler | Pending-entry tests pass; the next open-session entry produces no "reconciliation blocked" email and a `done` run |
| 3 | R03: default `(5, 30)` timeouts on the trading and data sessions; log cycle duration | Timeout tests pass; the log shows per-cycle duration |
| 4 | Full suite, restart the bot, watch one live session | 841+ tests pass; `manage.ps1 status` HEALTHY; no false alerts |

### Phase 2 — Bookkeeping and dashboard · v0.25.3 (patch)

| Step | Work | Done when |
|---|---|---|
| 1 | R05: closing context manager, `timeout=30`, run `_ensure_tables` once per process | Lock-contention and close tests pass; running a backtest during a live cycle raises no `database is locked` |
| 2 | R06: count and log only actionable signals, or store the skip reason | "N signals" matches the entries attempted |
| 3 | R08: rebuild `/api/bot-orders` from stored order ids with nested legs | Stop and TP fills appear on the dashboard for trades 73–78 |
| 4 | R09: comment and doc fixes, dead-code removal, remove the merged worktree | `grep` finds no stale comments; `git worktree list` shows main only |
| 5 | Restart the dashboard and the bot; verify at http://192.168.0.191:8004 | Both HEALTHY |

### Phase 3 — Execution timing · v0.26.0 (minor: changes when trades happen)

| Step | Work | Done when |
|---|---|---|
| 1 | R07: schedule-aligned wakeups at fill times, with the 30-min reconciliation cadence in between | Unit tests on the wakeup calculation (DST, early close, holiday) |
| 2 | Persist `fill_delay_sec` and `fill_vs_model_pct` on each trade | The dashboard shows median live slippage against the model |
| 3 | Run for two weeks and compare with the backtest's fill assumption | A written note in `research/experiments.md` |

### Phase 4 — Risk research · version depends on the outcome

| Step | Work | Done when |
|---|---|---|
| 1 | R04 step 1: generic group exposure cap, default off, in live sizing and `run_annual_portfolio` | Parity test shows live and backtest enforce the same cap; default runs match current baselines exactly |
| 2 | R04 step 2: cap sweep over 2020, 2022 and 2024–26, with every variant passed to `evaluate_search` | An experiment logged with evidence; a KEPT or REJECTED verdict and its drawdown/P&L trade-off |
| 3 | Ensemble simplification and the trend-break exit, as separately counted experiments | Each logged with `evaluate(..., trials=N)` evidence |
| 4 | Revisit the live-readiness verdict once Phases 1–3 are live and the shared-account item is decided | Updated `live-readiness` doc |

Phase 4 does not change live parameters unless an experiment passes. Phases 1–3
are correctness and operations work and need no research sign-off.
