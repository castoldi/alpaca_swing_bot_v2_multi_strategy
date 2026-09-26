# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ⚠️ RULE: never start a second instance

The bot and dashboard are **singletons**. Two `--loop` bots running at once caused a flood of duplicate emails. Always go through `scripts/manage.ps1` — it checks for a live healthy instance and refuses to spawn a duplicate. Never call `python bot.py --loop` or raw `uvicorn` directly.

```powershell
pwsh scripts\manage.ps1 status
pwsh scripts\manage.ps1 start-bot                        # ensemble @30m (idempotent)
pwsh scripts\manage.ps1 start-bot -Strategy regime -Interval 60
pwsh scripts\manage.ps1 restart-bot                      # use after editing bot.py
pwsh scripts\manage.ps1 restart-dashboard                # use after editing dashboard/*
pwsh scripts\manage.ps1 stop-bot
pwsh scripts\manage.ps1 stop-dashboard
```

Or use the root shortcuts: `start.bat`, `stop.bat`, `restart.bat` (all delegate to manage.ps1).

`manage.ps1` uses `.venv\Scripts\pythonw.exe` (no console window) with fallback to `python.exe`.

## ⚠️ RULE: restart after changes

After editing any file in `dashboard/` or `bot.py`, restart via the manager and confirm HEALTHY:

1. `pwsh scripts\manage.ps1 restart-dashboard` → verify http://192.168.0.191:8004
2. `pwsh scripts\manage.ps1 restart-bot -Strategy <strategy>`
3. `pwsh scripts\manage.ps1 status` — confirm both HEALTHY in your response.

## ⚠️ RULE: version, changelog, commit + push on every change

1. Add bullet(s) to `CHANGELOG.md` under the current version's Added/Fixed/Changed.
2. Bump version for any user-visible/behavioural change: `pwsh scripts\version.ps1 -Bump patch|minor|major`
3. Commit AND push — never leave committed-but-unpushed.
4. The `post-commit` hook auto-tags `v<version>+build<N>-<datetime>` and pushes. If it reports a failure: `git push --follow-tags`.

```powershell
pwsh scripts\version.ps1            # current version + build #
pwsh scripts\version.ps1 -Builds    # list all build tags
# One-time after fresh clone:
git config core.hooksPath scripts/git-hooks
```

## Development commands

```powershell
# Activate venv (Windows)
.venv\Scripts\activate

# Backtests — run before opening dashboard or DB will be empty
python backtest_2024.py
python backtest_2025.py
python backtest_2026.py

# Backtest a single strategy
python backtest_2025.py --strategy ensemble
python backtest_2025.py --strategy breakout

# Pre-2016 history (2000 / 2008 bears) from IB Gateway -> cache feed "ibkr"
# See "Historical data sources" below.
python scripts/import_ibkr_history.py

# One-shot bot run (no loop)
python bot.py --strategy ensemble

# Dashboard (dev — avoid on prod; use manage.ps1 instead)
python -m uvicorn dashboard.server:app --host 0.0.0.0 --port 8004
```

## Historical data sources (backtest cache)

All backtest history lives in **one SQLite file: `cache/market_data.db`**
(git-ignored), table `bars`, partitioned by the `feed` column. Inspect coverage
with `MarketDataCache().status()` or the `coverage` table.

| `feed` | Source | Range | Timeframes | Refresh |
|--------|--------|-------|-----------|---------|
| `iex` | Alpaca IEX (live default, `MARKET_DATA_FEED`) | 2020-07+ | 4h, 1d | auto, read-through |
| `sip` | Alpaca SIP | 2016-01+ | 4h, 1d | auto, read-through |
| `yfinance` | Yahoo | 2007–2015 | 1d only | auto |
| **`ibkr`** | **IB Gateway via `ibkr_trading_bot`** | **1999+** (META 2012, TQQQ 2010, ARM 2023) | 4h, 1d | **import-only** |

**Need more backtest history (pre-2016, the 2000/2008 bears, new symbols)? Use
the IBKR connection.** The sibling project `C:\Data\ai_projects\ibkr_trading_bot`
keeps IB Gateway running (paper, port 4002). `ibkr_history.py` connects to it
**read-only with client id 241** (never 17/18/19/117 — those belong to the MNQ
bot) and `scripts/import_ibkr_history.py` writes the `ibkr` partition:

```powershell
python scripts/import_ibkr_history.py                               # TICKERS + TQQQ + SPY, 1d+4h, 1999→today
python scripts/import_ibkr_history.py --tickers QQQ XLK --timeframes 1d 4h
python scripts/import_ibkr_history.py --force                       # re-download (e.g. to extend to today)
```

- Resumable: a series already covering `--start` is skipped unless `--force`.
- Reading it: `MarketDataCache().get_bars(sym, start, end, "4h", feed="ibkr")`.
  It never re-fetches on read; asking past the imported range returns only what
  is stored. Backtest runners still read `MARKET_DATA_FEED` (iex/sip) — point a
  research script at `feed="ibkr"` explicitly.
- 4h bars are rebuilt from IBKR 1-hour extended-hours bars into Alpaca's UTC
  buckets (never IBKR's session-anchored "4 hours" bars) and dividend-adjusted
  per day. Matches SIP 40/40 buckets; prices within ~0.1%.
- Differences vs SIP: volume excludes odd lots (~65% of SIP recently, ~95% in
  2016); pre-market buckets are sparse before ~2017.
- The Gateway is shared with the live MNQ bot — requests are spaced 2s apart;
  do not lower that. IBKR can also serve 1-minute bars (2016+) if a future
  execution model needs them (~10h+ download; not imported yet).
- `ib_async` 2.1.0 pins `tzdata<2026`; install with `--no-deps` (see
  `requirements.txt`).

**Earnings history** (Trend Pullback / Ensemble backtests): `cache/earnings.db`
holds live observations from 2026-09-14 plus a labelled historical import:
`python scripts/import_earnings_history.py [--force]`. See docs/earnings-policy.md.

## Architecture

```
config.py          StrategyType enum + StrategyParams frozen dataclass (all tuning knobs)
data_feed.py       Alpaca 4h bar fetcher → normalised OHLCV DataFrame
strategy.py        Indicators + 6 entry checkers + shared exit engine (simulate_exit_scaleout)
bot.py             Live loop: fetch data → check entry → place Alpaca orders → reconcile exits
backtest_20XX.py   Annual backtest runner (per-year scripts, all share the same logic shape)
dashboard/
  db.py            SQLite CRUD (trades, signals, bot_runs, backtest_runs, experiments)
  server.py        FastAPI on :8004 — /api/* endpoints + serves Plotly HTML reports
  index.html       Dark-theme SPA (Home tab + Strategies tab, Plotly charts)
  bot_hooks.py     Bridge: db ↔ bot (called after each order)
runtime.py         PID registration + heartbeat writer (run/bot.pid, run/bot.heartbeat)
notifier.py        Gmail SMTP alerts
```

**Data flow:**
`data_feed.fetch_4h` → completed bars only → `strategy.add_indicators` → `strategy.check_entry_*` → signal → slippage guard vs live price → protected bracket entry (market buy + OCO TP/SL) → `db.save_trade` + cash/slot reservation + real fill price → `bot._reconcile_and_exit` (all strategies) → `db.close_trade`

**Backtest flow:**
`download_history` → `collect_backtest_candidates` (strategy signals plus single/scaled exit paths) → `run_annual_portfolio` (20% whole-share sizing, cash/slot limits, realized-P&L compounding) → HTML report + `db.finish_backtest_run`

## Strategy architecture

All 8 strategies live in `strategies/`. The six bracket strategies exit through one protected bracket per entry (TP3 + SL at the broker, plus a breakeven-gated time stop); `sma_50_cross` and `tqqq_momentum` use a dedicated signal exit with an emergency stop (`exit_mode = "signal_with_stop"`). There is deliberately **no scale-out in live trading**: Alpaca rejects extra concurrent sell legs (403 40310000) and the single bracket also backtested better across 2024–2026.

**Ticker scoping**: `config.TICKERS` is the shared universe every strategy trades by default. A strategy may declare `tickers` to scope itself to specific symbols — `tqqq_momentum` does this for `LEVERAGED_TICKERS`, which both keeps it on TQQQ and keeps every other strategy off it. Resolve a universe with `strategy_universe(strategy, TICKERS)`, never by reading `TICKERS` directly in a per-strategy loop.

| Strategy | Key entry condition | SL | TP | Max hold |
|----------|--------------------|----|-----|---------|
| `trend_pullback` | Price > SMA(50), RSI dipped < 55, bounce bar. Earnings filter (3d before skip). | 10% | 2×ATR [3%–8%] | 5d |
| `breakout` | Intrabar high breaks prior 20-bar high; ≥1.5× volume; range ≤1.3× prior average; RSI ≥50 rising | 8% | 3×ATR [5%–15%] | 7d |
| `mean_reversion` | Price > SMA(50), ≥0.5% below SMA(20), RSI reached ≤50 in 7 bars, bounce | 7% | 1.5×ATR [1.5%–5%] | 3d |
| `momentum_macd` | MACD hist just crossed above 0, RSI > 50 rising, price > SMA(20) & SMA(50) | 9% | 2.5×ATR [4%–12%] | 6d |
| `regime` | EMA regime: risk-on dips (12% stop), risk-off oversold bounces (7% stop), neutral trend-like (10% stop) | adaptive | ATR-based [3%–8%] | 5d |
| `ensemble` | Weighted vote ≥0.30 (regime 35%, MACD 25%, trend 20%, breakout 15%, MR 5%); Regime alone qualifies; `ensemble_min_votes` (default 1) | 9% | 2.5×ATR [4%–12%] | 6d |
| `tqqq_momentum` | **TQQQ only.** TSI(25,13,13) crosses above its signal line | 8% (gap insurance) | none — exits on 4h close < EMA(50) | n/a |

`bot.py` entry dispatch: `get_entry_checker(strategy)` returns the matching `check_entry_*` function.

All strategies share: TP reachability filter (TP1 within 4 ATRs of the entry,
measured on the strategy's own candles — 4 bar-ATRs on 4h),
one position per ticker, whole-share entries capped at 20% of equity
and cash, five positions maximum, no margin, an entry slippage guard
(skip if live price drifts >1.5% from the signal close), a daily-loss kill
switch (the bot's own loss since the previous close reaching 3% of its
capital base halts new entries; account-wide drop only as a fallback), and
one protected bracket exit (TP3 + SL) per entry regardless of quantity. Annual
backtests start at $1,000, compound within the year, and reset each January.

**Virtual capital allocation** (`bot_capital_allocation`, default $100,000,
override `BOT_CAPITAL_ALLOCATION` in `.env`, 0 = off): the Alpaca key is shared,
so live "equity" is this fixed allocation (capped at real account equity), not
the account. Spendable cash = min(real account cash, allocation − this bot's own
open cost basis); an unreadable ledger means nothing is spendable. The same base
drives the leveraged/group caps, the kill-switch denominator and the ledger's
return %. Not compounded. Backtests ignore it.

**Leveraged exposure cap**: total notional across `LEVERAGED_TICKERS` is capped
at `max_leveraged_exposure_pct` (default 20%) of equity, enforced identically in
live sizing and `run_annual_portfolio`. The 5-position limit is count-based and
correlation-blind — 5 × 20% = 100% of equity — so without this cap a multi-ETF
leveraged universe could put the whole account into 3x instruments at once.
The default equals one 20% position, so **adding a leveraged ticker cannot raise
risk until this number is deliberately raised**. `bot._open_leveraged_notional`
fails closed: an unreadable position is charged the full cap.

**Correlated-group caps** (`exposure_groups`, default empty = off): the same
mechanism for any declared group, e.g. `(("semis", ("NVDA","AMD","ARM"), 0.40),)`,
enforced in live sizing and `run_annual_portfolio`. Pick values only from
`research/group_cap_experiment.py`, never by guess.

**Ensemble warmup**: needs 60+ bars before first signal. Regime needs 50+ for EMA(50).

## Bot trading hours

The loop only calls `run_once` while **Alpaca's market clock reports the market open** (handles holidays and early closes; fallback window 09:30–16:00 ET weekdays if the clock API fails). Outside the session it heartbeats normally and logs `"Outside trading hours"`. The candle timeframe is **4h** and `data_feed.completed_bars` drops the still-forming bucket, so signals only ever come from completed candles — a 30-min loop interval is well-matched.
A 4h bucket counts as complete 60 s after it ends (`data_feed.BAR_SETTLE`), and the
loop also wakes 90 s after every modelled fill time (`bot._seconds_until_next_pass`),
so live entries land within ~2 min of the backtest's first-minute fill.

## Dashboard

Port **8004**. Always give the user http://192.168.0.191:8004 (their mobile network address) — never `localhost`, which is unreachable from their phone.

**Access token:** when `DASHBOARD_TOKEN` is set in `.env`, LAN requests need it
once per device (`http://192.168.0.191:8004/?token=<value>`, then a cookie).
Requests from this machine never do, so the watchdog probe keeps working.

Routes: `/` (Home + Strategies tabs), `/backtest-2024`, `/backtest-2025`, `/backtest-2026`.

Key API endpoints: `/api/summary`, `/api/trades`, `/api/positions`, `/api/backtest-results`, `/api/backtest-history`, `/api/strategy-examples` (cached 4h candlestick charts), `/api/bot-orders`
(own orders incl. bracket legs), `/api/execution-quality` (live fill vs modelled fill).

Run backtests before first open or the DB will be empty.

## Runtime state (`run/`, git-ignored)

| File | Written by | Contents |
|------|-----------|----------|
| `run/bot.pid` | `bot.py` via `runtime.py` | loop process PID |
| `run/bot.meta.json` | `bot.py` | pid, strategy, interval, started_at |
| `run/bot.heartbeat` | `bot.py` every loop pass | ISO timestamp |
| `run/dashboard.pid` | `manage.ps1` | uvicorn PID |

Health model: bot = pid alive AND heartbeat fresh within ~2 intervals; dashboard = HTTP probe succeeds. Each bot appears as **two** python processes (venv launcher shim + uv-managed child) — that pair is one instance.

## Pitfalls

- **High-priced stocks**: if one share costs more than the 20% equity allocation or available cash, the bot skips it entirely (no order, no email).
- **simulate_exit uses signal prices directly**: SL/TP on the `EntrySignal` object are authoritative — do not recalculate from params inside `simulate_exit_scaleout`.
- **Ensemble threshold**: 0.30 (tightened from 0.25 on 2026-05-28) — in `strategy.py` around `check_entry_ensemble`.
- **Alpaca paper hardcoded**: `paper=True` is set in both `bot.py` and `server.py` regardless of `.env`.
- **Windows venv**: `.venv\Scripts\activate` (not `source .venv/Scripts/activate`).
- **DB empty**: backtests write results to `dashboard/swing_bot_v2.db` — run them before opening the dashboard.
- **Ledger integrity**: unique indexes reject a duplicate entry order id and a
  sell claimed by two trades (`sqlite3.IntegrityError` = a real double claim; fix
  the caller, never drop the index). Corrupt rows go to `trades_quarantine` via
  `scripts/quarantine_trades.py` (dry run by default, backs up the DB), never
  `DELETE`. `external_liquidation` exits count in P&L dollars but not in win
  rate/PF/trade counts.
- **Shorting is settled — don't re-propose it.** Short-only (`research/short_only_mirror.py`,
  2026-09-26: the real strategy code on inverted prices) lost 10 of 11 years
  2016–2026 (ensemble, −0.80%/trade, t = −5.13) and 19 of 22 years on NVDA
  2005–2026; only 2022 won. Bear-only shorting also failed (docs/bear-market-playbook.md §4).
  For bear protection, pair `ensemble` with `tqqq_momentum`. Run that script with
  `ensemble` as an argument: all six strategies take >10 min and can hit low memory.

## Keep-alive watchdog

> Full doc: [docs/keepalive.md](docs/keepalive.md)

`keep_alive.py` runs every 30 minutes via Windows Task Scheduler using `pythonw.exe` (no window). It checks bot + dashboard health and calls `manage.ps1` to restart whichever is down. If both are healthy it exits silently in under one second.

**One-time setup (Admin PowerShell — do this once per machine):**

```powershell
pwsh scripts\setup_keepalive_task.ps1          # register task
Start-ScheduledTask -TaskName AlpacaSwingBotKeepAlive   # fire immediately to test
Get-ScheduledTaskInfo -TaskName AlpacaSwingBotKeepAlive  # confirm LastRunTime + LastTaskResult=0
```

**Logs** (git-ignored):

```powershell
Get-Content logs\keepalive.log -Tail 50        # watchdog decisions
Get-Content logs\keepalive_manage.log -Tail 50 # manage.ps1 output from restarts
```

**Remove task:**

```powershell
pwsh scripts\setup_keepalive_task.ps1 -Unregister
```

**AI rules for this system:**
- Never edit `keep_alive.py` to start the bot directly — always delegate to `manage.ps1`.
- The heartbeat max-age formula `(interval * 2 * 60 + 300)` in `keep_alive.py` must match `manage.ps1 Get-BotHealth`. Change both together.

## Research loop

1. Edit `strategy.py` (new signal or param tweak)
2. Run `python backtest_2025.py` and `python backtest_2026.py`
3. Compare in dashboard or DB; both years must improve — **necessary, not sufficient**
4. Price the result against the search that found it (see below)
5. Log via `db_mod.log_experiment(..., evidence=report.as_dict())`

### ⚠️ RULE: count your trials before keeping a change

"Both years improved" is two observations with no correction for how many
variants you tried to get there. Test enough knobs and something always clears
it. `research/significance.py` implements the Harvey & Liu (2020) correction:

```python
from research.significance import evaluate, evaluate_search

# One candidate change, N variants looked at along the way.
report = evaluate([t.pnl_pct for t in trades], trials=N)
print(report.summary())

# A full parameter sweep — pass EVERY variant, losers included.
report, bhy_p = evaluate_search({label: [(t.entry_date, t.pnl_pct) for t in tr]
                                 for label, tr in sweeps.items()})
```

- `trials` is the honest count of parameter values, strategy variants and data
  slices considered before settling on this one. **Undercounting it is the
  easiest way to fool yourself — when in doubt, round up.**
- `research/optimizer.py:random_search` now returns `(results, report)` and does
  this automatically; sorting N configs by P&L and taking the top is a maximum
  over N correlated tests, so the winner's raw t-statistic is not comparable to
  a t > 2 bar. **Expect most sweeps to fail. That is the correct outcome.**
- `log_experiment` warns when a `KEPT` verdict arrives without evidence.
- Caveat that belongs in every write-up: trades overlap in time across
  correlated tickers, so per-trade t-statistics are optimistic. The month-block
  bootstrap in `evaluate_search` absorbs some of this; `evaluate` does not.
## Process Idempotency
- Before creating or modifying any startup, scheduler, watchdog, keepalive, dashboard, bot, strategy, or other long-running process script, make it idempotent: repeated manual, scheduled, Startup-folder, Hermes, or agent-monitor invocations must adopt the existing healthy process instead of starting a duplicate.
- Use a single-instance lock plus a real process identity check such as command line, port owner, and health endpoint; verify PID files against that identity and never rely on a PID file alone.
- Windows Scheduled Tasks for this project must use `MultipleInstances IgnoreNew`; avoid overlapping scheduled tasks for the same service unless every launch path shares the same guard.
- When replacing an unhealthy process, kill or adopt only matching project command lines/ports so unrelated processes are not touched and phantom processes are not left behind.

## No Visible Windows
Scheduled tasks, keepalives, watchdogs and bots on this machine run while the owner is working. A console window that pops up, even for a second, steals focus and interrupts them, so treat one as a bug. This applies to every project. It covers background automation only: a window the owner explicitly asks to see (for example the nfl-dashboard live-draft console) is fine.
- **Never make a console program the action of a Task Scheduler task.** That means `powershell.exe`, `pwsh.exe`, `cmd.exe`, `python.exe`, `.bat`/`.cmd`, `node`, `uv` and `git`. Under the default Interactive logon, Windows opens a console window on the desktop every time the task fires. `-WindowStyle Hidden` does not prevent it: it hides the window only after the window has appeared. Use one of these instead:
  - Python: `-Execute` a real `pythonw.exe`, preferably the project's `.venv\Scripts\pythonw.exe`.
  - PowerShell or another console tool: a `pythonw.exe` wrapper that runs it with `CREATE_NO_WINDOW` and returns its exit code (reference: `watch-vault/scripts/windowless.py`). `conhost.exe --headless powershell.exe -File ...` also shows no window, but it always reports exit code 0, so Task Scheduler's "Last Run Result" becomes meaningless.
  - `-LogonType S4U` runs the task in a non-interactive session, so nothing can appear. Use it only for jobs that need neither DPAPI secrets (Windows Credential Manager, so no `git push` through the credential manager) nor network shares.
- **Every child process started from Python gets `creationflags=subprocess.CREATE_NO_WINDOW`**, plus a `STARTUPINFO` with `SW_HIDE` where practical. This matters most under `pythonw.exe`: it has no console, so every console child it starts (`python.exe`, the venv trampoline, `claude`, `git`, `powershell`) would otherwise open a window of its own. Never add `DETACHED_PROCESS`. Windows ignores `CREATE_NO_WINDOW` when the two are combined, and the detached child's own console children then open visible windows.
- **From PowerShell**, start long-running Python with `Start-Process pythonw.exe -WindowStyle Hidden`. Run short console tools inline (`& python ...`) so they share the already-hidden console.
- **Verify after registering or changing a task.** No task action may be a console program unless its logon type is S4U. Run the task once with `Start-ScheduledTask` and confirm nothing appears. To list every task's logon type and action:
  `Get-ScheduledTask | ? TaskPath -notlike '\Microsoft\*' | % { "$($_.TaskName) | $($_.Principal.LogonType) | $($_.Actions.Execute)" }`
