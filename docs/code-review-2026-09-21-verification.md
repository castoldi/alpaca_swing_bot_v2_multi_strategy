# Verification of the F01–F16 remediation

**Reviewed:** 2026-09-21
**Code baseline:** `90dfd9f` (v0.24.21), clean working tree
**Subject:** [code-review-2026-09-05.md](code-review-2026-09-05.md), which says all
16 findings are fixed.
**Method:** read every fix commit's diff (`b9dd867` … `2c8bf1d`) and the current
source. Ran the full suite. Checked specific claims against SciPy, the market-data
cache, the earnings archive and the live trade ledger. No orders were placed, no
services were restarted and no backtests were run. The backtest scripts overwrite
database results, so none were run.

## Resolution — all nine items fixed in v0.25.0

| ID | Fix | Regression tests |
|---|---|---|
| V01 | `signal_cursor` table plus a per-bar fill window (`bot._signal_is_actionable`); each bar is evaluated once and the cursor advances before any order work | `tests/test_verification_fixes.py` (re-entry, slippage retry, new bar, cursor failure, real window) |
| V02 | Every bar completed since the cursor is evaluated, oldest first; signal exits latch from any bar after the entry signal (`_signal_exit_reason`) | same file (overnight close bar, no history replay, exit latch) |
| V03 | Live stop uses `strat_obj.stop_loss_fraction(PARAMS)` | TQQQ 8% stop test |
| V04 | `stop_identity` tolerates `AccessDenied` from an exiting process and kills after a wait timeout | 3 new `test_service_manager.py` cases (fail on the old code) |
| V05 | `ensemble_min_votes` default back to 1 (pre-F16 live behaviour); description corrected; 2 votes stays available for the ablation | `test_default_ensemble_restores_pre_f16_regime_only_entry` |
| V06 | `scripts/import_earnings_history.py`: labelled weekly snapshots from 2002 (ARM from 2023) up to the first live observation; 4,693 imported | `tests/test_import_earnings_history.py`; real NVDA 2024–25 check: blocks only earnings-week bars, no unknowns |
| V07 | Per-trade reconciliation failures are returned, recorded on the run, and emailed once per trade per day; a new protection audit alerts on any owned position without a live stop | alert-dedupe, gap-detection and audit tests |
| V08 | Kill switch uses the bot's own P&L since the previous close over yesterday's equity; if that is unknown it falls back to the account-wide drop | 4 rewritten `test_bot_risk_guards.py` cases |
| V09 | Remediation doc corrected (F03 status, stale "next" line, repeated boilerplate, unlinked review claims) | — |

Still open: regenerate the corrected baselines, then run the V05 ablation (1 vs 2
votes, regime-only, equal weights) through `evaluate_search`.

## Verdict

**Most of the fixes are correct. The claim that nothing remains open is not.**
The suite passes (**805 passed**, one existing `websockets.legacy` warning), and
the fixes I checked do what the document says:

| Finding | Verified | Notes |
|---|---|---|
| F01 ownership | ✅ | Linked bracket and replacement fills are recorded and finalized before any close decision. Broker quantity is used only as an upper limit. |
| F02 GTC protection | ✅ with caveat | GTC bracket, OTO and OCO orders; repair uses exact linked IDs and persists the client ID before submitting. Stuck states have no alerting (V07). |
| F03 manager | ⚠️ incomplete | Locking and identity checks are sound. `restart` still fails on its stop path (V04). |
| F04 next-open basis | ✅ | Fill price and time drive shares, cash, P&L and holding time. |
| F05 gap and session clock | ✅ | An opening gap through the stop fills at the open. Minute-level XNYS clock; stop is assumed first when a minute touches both barriers. |
| F06 earnings | ✅ as code / ⚠️ as research | The policy is correct, but the archive starts on 2026-09-14 (V06). |
| F07 daily-loss backtest | ✅ | The baseline is replayed through the previous session close and valued only with prices observable at the time. |
| F08 t-statistic | ✅ | `[1%,2%,3%,4%]` gives t = 3.8730, p = 0.015233, identical to SciPy. Negative and tiny-variance cases also match. |
| F09 cache | ✅ | Full-range replacement inside a transaction, with snapshot fingerprints. |
| F10 feed | ✅ | IEX is used end to end. |
| F11 holding period | ✅ | Live and backtest share one XNYS deadline measured from the fill time. |
| F12 partial entries | ✅ | A terminal-entry refresh runs before every exit branch. |
| F13 exception isolation | ✅ | Reconciliation runs in its own guarded phase. |
| F14 RSI | ✅ | Rising, falling and flat series give 100, 0 and 50. Warmup values stay NaN until the 15th close. |
| F15 optimizer | ✅ | The optimizer uses the annual runner with a per-strategy parameter space and a frozen dataset. |
| F16 predicates | ⚠️ | The math is right. One fix changed the live strategy without evaluation (V05). |

The new findings below are ranked by severity. V01–V03 are live-trading
parity defects that neither the original review nor its fixes addressed. V01 has
direct evidence in the live ledger.

| ID | Priority | Finding |
|---|---|---|
| V01 | **P1** | The live bot re-enters on the same completed signal bar repeatedly, and enters hours or days late |
| V02 | P2 | Bars that complete while the market is closed are never evaluated live, but the backtest trades them |
| V03 | P2 | Live TQQQ uses a 10% emergency stop; the backtest, optimizer and docs use 8% |
| V04 | P2 | F03 incomplete: `restart-bot` / `restart-dashboard` fail with `(pid=N)` |
| V05 | P2 | F16 changed the live Ensemble entry rule (≥2 votes) without any backtest |
| V06 | P2 | The earnings archive starts on 2026-09-14, so every earlier backtest disables Trend Pullback and the Ensemble's trend vote |
| V07 | P3 | States that need an operator are only logged; a position can sit without protection and nobody is told |
| V08 | P3 | The live kill switch measures the shared 9-project account; the backtest models an isolated one |
| V09 | P3 | Remediation document accuracy |

---

## V01 — Same signal bar traded repeatedly; late entries (P1)

**Location:** [bot.py](../bot.py) `run_once`, lines 718–765 (`idx = len(df) - 1`
at 741; the only duplicate guard is `db_mod.get_open_trade` at 763).

Each 30-minute cycle evaluates the latest completed bar. A 4h bar stays the
latest for up to 4 hours, and one that completes after the close stays the
latest until the next session is under way. Nothing records that a bar has
already been acted on. The only guard is "no *open* trade for this ticker". So:

1. **Re-entry after an exit.** If the bracket or time stop closes the trade while
   the same bar is still the latest, the next cycle buys again on the same signal.
2. **Late entries.** If cycle 1 skips the entry because the price drifted more
   than 1.5%, cycles 2–8 retry it. Any cycle where the price comes back into range
   enters, up to hours after the signal.

The backtest (`backtest_execution.collect_session_candidates`) creates **one**
candidate per signal bar. It fills at the first regular-session minute after the
bar completes and applies the slippage guard only there. Live therefore takes
trades the backtest never modelled.

**Evidence in the live ledger** (`dashboard/swing_bot_v2.db`):

| Trade | Signal bar (UTC) | Created (UTC) | Exit |
|---|---|---|---|
| 63 META ×34 | 2026-09-04 16:00 (Fri) | 2026-09-08 14:35 (Tue) | time_stop 15:05, −$3.74 |
| 64 META ×34 | **same bar** | 2026-09-08 15:36 | time_stop 17:06, +$7.14 |

The first entry came one hour after Tuesday's open, from Friday's bar. The
backtest would have filled it at 13:30 UTC or not at all. After it exited, the
bot bought again on the same bar 30 minutes later. The instant time stop was the
F11 bug, now fixed, but the re-entry path is unchanged. Older history is worse:
NVDA has 22 trades on the 2026-07-02 16:00 bar and 5 on each of two 2026-06-29
bars.

**Solution:** make a signal bar actionable exactly once, at the same moment the
backtest acts on it.

- Persist a per-(strategy, ticker) cursor holding the last signal bar evaluated.
  A new table or a column on `bot_runs` both work. Skip any bar at or before the
  cursor.
- Advance the cursor once the bar has been evaluated, whether it produced an
  entry, a skip (slippage, TP-reachability, sizing, tax) or no signal. Do not
  advance it on a data or exception failure.
- Optionally reject a bar whose `signal_availability` is more than one loop
  interval before the first regular-session minute after availability. This
  bounds lateness when the bot was down, which the backtest also cannot model.
- Regression tests: (a) an exit followed by a later cycle on the same bar places
  no order; (b) a skip for slippage in cycle 1 followed by an in-range price in
  cycle 2 places no order; (c) a newly completed bar is still evaluated.

## V02 — Bars that complete outside market hours are skipped live (P2)

**Location:** same as V01, plus
[backtest_execution.py](../backtest_execution.py) `collect_session_candidates`.

The IEX 4h buckets in the cache start at **12:00, 16:00 and 20:00 UTC** (checked:
1962 / 1962 / 1021 bars since 2025-06-01). In summer the 16:00 bucket is the
regular-session close bar, 12:00–16:00 ET, and it completes at the close. The bot
only runs while the market is open, so the next morning the latest bar is the
after-hours 20:00 bucket when one exists. That happens on about half of all days.
**Close-bar signals are then never evaluated live.** The backtest evaluates every
bar and fills close-bar signals at the next open, so it trades a class of
entries the live bot mostly cannot take. The same applies in winter with shifted
buckets.

**Solution:** combine this with V01. Instead of only `iloc[-1]`, live evaluates
**every completed bar after the cursor**, oldest first, and acts on the first
signal only if the current time is still inside the backtest's fill window. The
signal is then subject to the same slippage guard against the signal close. This
reproduces the backtest rule: every bar is considered once and fills at the first
eligible minute. The alternative is to make the backtest simulate the live
scheduler, keeping only bars that are latest at a live check time. That is
simpler but throws away signals. Pick one policy, write it in
`docs/backtest-execution.md`, and test it on a Friday-close → Monday-open case
and a holiday case.

## V03 — Live TQQQ emergency stop is 10%, not 8% (P2)

**Location:** [bot.py](../bot.py) lines 891–894:

```python
if strat_obj.exit_mode == "signal_with_stop":
    sig.stop_loss = market_ref * (1.0 - PARAMS.sma_cross_stop_loss_pct)
```

This hardcodes the SMA-cross stop (10%) for **every** signal-with-stop strategy.
`TqqqMomentumStrategy.stop_loss_fraction` returns `tqqq_stop_loss_pct` (8%). The
backtest uses it (`backtest_execution.py`: `strategy.stop_loss_fraction(params)`),
and so do the F15 optimizer space and CLAUDE.md. On a 3× ETF, live gap
insurance is looser than tested, and the optimizer's `tqqq_stop_loss_pct`
sweep has no effect on live trading.

**Solution:** `sig.stop_loss = market_ref * (1.0 - strat_obj.stop_loss_fraction(PARAMS))`.
Add a test that submits a `tqqq_momentum` entry through a fake client and asserts
the OTO stop equals 92% of the reference price. The replacement stop in
`_ensure_owned_protection` reuses `trade["stop_loss"]`, so it is correct once the
entry is.

## V04 — Manager restart fails on its own stop path (P2, F03 incomplete)

**Location:** [service_manager.py](../service_manager.py) `stop_identity`,
lines 93–112.

The remediation record admits that `restart` "returned a PID-only manager error"
during the F10 and F15 activations (CHANGELOG line 551). Each time a human then
ran a manual `start-*`. The document classifies this as a separate follow-up
while marking F03 fixed. The cause is identifiable:

- `stop()` terminates the newer interpreter first, which makes the venv launcher
  exit by itself. When `stop_identity` then calls `proc.terminate()` on the
  launcher while it is exiting, Windows `TerminateProcess` returns access denied.
  psutil raises `AccessDenied(pid)`, and **`str(psutil.AccessDenied(1234)) == '(pid=1234)'`**
  (checked with psutil 7.2.2): exactly the "PID-only" message. `main()` catches
  `psutil.Error` and prints `Manager refused operation: (pid=…)`.
- The same method also lets `psutil.TimeoutExpired` escape from `proc.wait(timeout=8)`.

CLAUDE.md requires `restart-bot` after every `bot.py` edit, so the prescribed
workflow fails every time.

**Solution:**

```python
try:
    proc.terminate()
except psutil.AccessDenied:
    pass  # an exiting process refuses TerminateProcess; verify below
try:
    proc.wait(timeout=8)
except psutil.TimeoutExpired:
    proc.kill()
    proc.wait(timeout=5)
```

After the child stops, wait briefly (`psutil.wait_procs`) for the verified
launcher to exit by itself before terminating it. Keep the existing
post-stop `self.processes(service)` check as the authority. Add a regression test
with a fake process whose `terminate()` raises `AccessDenied` and then reports
gone. It must pass, while a process that stays alive must still be refused.

## V05 — F16 changed the live Ensemble rule without evaluation (P2, process)

**Location:** [strategies/ensemble.py](../strategies/ensemble.py) line 60
(`len(active) < p.ensemble_min_votes`), [config.py](../config.py) line 225.

The original review's F16 correction said: *"Treat added confirmation
requirements as strategy changes requiring separate evaluation."* Its research
table listed "a two-member requirement" as an **experiment** to ablate against
regime-only entries. The remediation instead made the description authoritative:
it added `ensemble_min_votes = 2` and deployed it to the running bot, which
trades `ensemble`. It was released with no backtest, no trial count and no
`log_experiment`. That contradicts the CLAUDE.md research loop. Regime has the
highest weight (0.35) and was previously the single largest source of entries.
Blocking its solo entries is probably the largest behaviour change in the whole
remediation.

Mean Reversion got the opposite treatment: there the predicate was authoritative,
and the Bollinger claim was dropped from the description. The Breakout range fix
is a legitimate no-op-parameter correction, but it also changes the Breakout vote
inside the Ensemble.

**Solution:** make this a deliberate decision. Recommended: set
`ensemble_min_votes = 1` so the pre-F16 entry behaviour is restored, and correct
the description to say "Regime alone can qualify". Then run the ablation the review
proposed on the corrected engine (1 vs 2 votes, regime-only, equal weights)
through `evaluate_search` with every variant counted. Promote the 2-vote rule only
if it passes. Either value is unvalidated on the corrected engine. The point is
that a live behaviour change should come from evidence, not from making code match
a description.

## V06 — Earnings archive gap disables Trend Pullback in every historical backtest (P2, research)

**Location:** [earnings_calendar.py](../earnings_calendar.py), `cache/earnings.db`.

The F06 policy suppresses Trend Pullback wherever no point-in-time schedule was
archived. The archive holds **45 snapshots, first observed 2026-09-14T13:49Z**.
Consequences:

- Every backtest before 2026-09-14 (all of 2016–2025 and most of 2026) has
  **zero** Trend Pullback trades. `random_search('trend_pullback', …)` gives
  zero trades for every configuration.
- Backtest Ensemble loses its 0.20 trend vote. Together with V05's 2-vote rule, the
  historical Ensemble is a four-member vote while live is five. The document's
  "next: regenerate corrected baselines" will therefore produce Ensemble and Trend
  baselines that do not describe the live strategy.

The strictness is defensible; running unaware of it is not. **Solution:** import a
historical earnings-date archive through the existing `CalendarStore` import API.
IBKR Wall Street Horizon via the sibling Gateway, yfinance `get_earnings_dates`
or Nasdaq history are all possible sources. Record it with an explicit provenance
label such as `source="historical_import", assumption="date known ≥5 sessions
ahead"`, because scheduled dates are normally public weeks ahead. Report
baselines both with the import and in strict mode, and state the approximation in
every write-up. Until then, label Trend and Ensemble historical results "earnings
filter unavailable".

## V07 — States that need an operator never alert (P3)

**Location:** [bot.py](../bot.py) `_reconcile_and_exit` lines 1276–1277 (per-trade
exceptions are logged and swallowed); `_owned_bracket_orders` line 1815; `_cancel_owned_bracket`.

The fail-closed design is right, but several of its end states leave a position
**without broker protection indefinitely** and send no notification:

- A protective submission whose response was lost and whose client ID then returns
  404 raises "operator reconciliation required" on every cycle. The old
  protection was already cancelled.
- Cancellation cannot be confirmed, or a controlled exit is definitively rejected
  after protection was removed. The retry repeats every cycle.

Per-trade exceptions never reach `run_once`'s `errors` list, so the "Bot V2 Error"
email does not fire.

**Solution:** return per-trade reconciliation failures to `run_once` and include
them in `errors`. Separately, send a notification at most once per trade per day
whenever a filled position has no live stop (no active linked `stop` order). That
covers exactly the "unprotected" state regardless of cause.

## V08 — Live kill switch reads the shared account (P3)

**Location:** [bot.py](../bot.py) `_daily_loss_pct` / `_load_live_sizing`.

F07 made the *backtest* guard measure this bot's isolated portfolio. Live still
compares `account.equity` with `account.last_equity` for the paper account that
nine projects share (see the shared-account note in memory and CLAUDE.md). Another
project's loss can halt this bot's entries, and this bot's own 3% loss can go
unnoticed inside a larger account. The two guards now measure different things.

**Solution:** compute daily loss from the bot's own ledger. `portfolio.Snapshot`
already tracks bot-owned equity. Alternatively, record in both places that the
live guard is account-wide until the separate account (deferred 2026-09-10)
exists.

## V09 — Remediation document accuracy (P3)

- F03 is marked **FIXED** while restarts fail as described in V04. It should read
  "partially fixed".
- The F02 section still says "**Next unresolved finding: F16**".
- The sentence "Numbered review findings F01–F16 are resolved" is repeated in 14
  sections. It hides which record is current.
- Every row says "independent review complete", but no review artifact is linked.
  Link the review notes or drop the claim.

## Recommended order

1. V03 (one-line fix) and V01/V02 together (signal cursor plus a shared fill-window
   policy). Then restart through the manager and confirm HEALTHY.
2. V04, so that step 1's restart works as documented.
3. V05 decision, then V06 import, **then** regenerate the baselines.
4. V07 alerting; V08 decision; V09 doc cleanup.
