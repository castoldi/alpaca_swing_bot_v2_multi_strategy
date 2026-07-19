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

1. `pwsh scripts\manage.ps1 restart-dashboard` → verify http://localhost:8004
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

# One-shot bot run (no loop)
python bot.py --strategy ensemble

# Dashboard (dev — avoid on prod; use manage.ps1 instead)
python -m uvicorn dashboard.server:app --host 0.0.0.0 --port 8004
```

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
| `breakout` | Price breaks 20-bar high, ≥1.5× avg volume, RSI > 50 rising | 8% | 3×ATR [5%–15%] | 7d |
| `mean_reversion` | Price > SMA(50) but < SMA(20), RSI < 50, near Bollinger lower band, bounce | 7% | 1.5×ATR [1.5%–5%] | 3d |
| `momentum_macd` | MACD hist just crossed above 0, RSI > 50 rising, price > SMA(20) & SMA(50) | 9% | 2.5×ATR [4%–12%] | 6d |
| `regime` | EMA(10)/EMA(50) regime: risk-on = buy dips, risk-off = oversold bounces, neutral = trend-like | adaptive | ATR-based [3%–8%] | 5d |
| `ensemble` | Weighted vote ≥ 0.30 across all 5 base strategies (regime 35%, MACD 25%, trend 20%, breakout 15%, MR 5%) | 9% | 2.5×ATR [4%–12%] | 6d |
| `tqqq_momentum` | **TQQQ only.** TSI(25,13,13) crosses above its signal line | 8% (gap insurance) | none — exits on 4h close < EMA(50) | n/a |

`bot.py` entry dispatch: `get_entry_checker(strategy)` returns the matching `check_entry_*` function.

All strategies share: TP reachability filter (target reachable in ≤4 ATR-days),
one position per ticker, whole-share entries capped at 20% of current equity
and cash, five positions maximum, no margin, an entry slippage guard
(skip if live price drifts >1.5% from the signal close), a daily-loss kill
switch (−3% vs yesterday's closing equity halts new entries for the day), and
one protected bracket exit (TP3 + SL) per entry regardless of quantity. Annual
backtests start at $1,000, compound within the year, and reset each January.

**Leveraged exposure cap**: total notional across `LEVERAGED_TICKERS` is capped
at `max_leveraged_exposure_pct` (default 20%) of equity, enforced identically in
live sizing and `run_annual_portfolio`. The 5-position limit is count-based and
correlation-blind — 5 × 20% = 100% of equity — so without this cap a multi-ETF
leveraged universe could put the whole account into 3x instruments at once.
The default equals one 20% position, so **adding a leveraged ticker cannot raise
risk until this number is deliberately raised**. `bot._open_leveraged_notional`
fails closed: an unreadable position is charged the full cap.

**Ensemble warmup**: needs 60+ bars before first signal. Regime needs 50+ for EMA(50).

## Bot trading hours

The loop only calls `run_once` while **Alpaca's market clock reports the market open** (handles holidays and early closes; fallback window 09:30–16:00 ET weekdays if the clock API fails). Outside the session it heartbeats normally and logs `"Outside trading hours"`. The candle timeframe is **4h** and `data_feed.completed_bars` drops the still-forming bucket, so signals only ever come from completed candles — a 30-min loop interval is well-matched.

## Dashboard

Port **8004**. Local: http://localhost:8004 — LAN: http://192.168.0.191:8004

Routes: `/` (Home + Strategies tabs), `/backtest-2024`, `/backtest-2025`, `/backtest-2026`.

Key API endpoints: `/api/summary`, `/api/trades`, `/api/positions`, `/api/backtest-results`, `/api/backtest-history`, `/api/strategy-examples` (cached 4h candlestick charts).

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
3. Compare in dashboard or DB; keep if both years improve
4. Log via `db_mod.log_experiment(...)` or `program.md`
## Process Idempotency
- Before creating or modifying any startup, scheduler, watchdog, keepalive, dashboard, bot, strategy, or other long-running process script, make it idempotent: repeated manual, scheduled, Startup-folder, Hermes, or agent-monitor invocations must adopt the existing healthy process instead of starting a duplicate.
- Use a single-instance lock plus a real process identity check such as command line, port owner, and health endpoint; verify PID files against that identity and never rely on a PID file alone.
- Windows Scheduled Tasks for this project must use `MultipleInstances IgnoreNew`; avoid overlapping scheduled tasks for the same service unless every launch path shares the same guard.
- When replacing an unhealthy process, kill or adopt only matching project command lines/ports so unrelated processes are not touched and phantom processes are not left behind.
