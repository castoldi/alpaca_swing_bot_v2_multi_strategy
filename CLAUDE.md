# Claude Instructions — Alpaca Swing Bot V2

## ⚠️ RULE: never start a second instance

The bot and the dashboard are **singletons**. Running two `--loop` bots at once
caused a flood of duplicate emails. **Always go through `scripts/manage.ps1`** —
it checks whether a live, healthy instance already exists and refuses to spawn a
duplicate. Never run `python bot.py --loop` or the raw `uvicorn` command directly
to start a long-running process; use the manager.

```powershell
cd C:\Data\ai_projects\alpaca_swing_bot_v2_multi_strategy

pwsh scripts\manage.ps1 status                       # what's running + health
pwsh scripts\manage.ps1 start-bot                     # ensemble loop @30m (idempotent)
pwsh scripts\manage.ps1 start-bot -Strategy regime -Interval 60
pwsh scripts\manage.ps1 stop-bot
pwsh scripts\manage.ps1 restart-bot                   # use after editing bot.py
pwsh scripts\manage.ps1 start-dashboard               # idempotent
pwsh scripts\manage.ps1 stop-dashboard
pwsh scripts\manage.ps1 restart-dashboard             # use after editing dashboard/*
```

`start-bot` / `start-dashboard` are **safe to call repeatedly** — if a healthy
instance is already running they do nothing. If the existing process is dead or
hung (stale heartbeat / no HTTP response) they replace it and sweep any orphan
processes first.

## Finding the PIDs (and health)

Runtime state lives in `run/` (git-ignored):

| File | Written by | Contents |
|------|-----------|----------|
| `run/bot.pid` | `bot.py` (via `runtime.py`) | loop process id |
| `run/bot.meta.json` | `bot.py` | pid, strategy, interval, started_at, cmd |
| `run/bot.heartbeat` | `bot.py` (every loop pass) | ISO timestamp — proves it's looping |
| `run/dashboard.pid` | `manage.ps1` | uvicorn process id |
| `run/dashboard.meta.json` | `manage.ps1` | pid, port, started_at |

Quick ways to find the PIDs:

```powershell
pwsh scripts\manage.ps1 status              # preferred — shows PID + HEALTHY/UNHEALTHY/STOPPED
Get-Content run\bot.pid                      # bot loop PID
Get-Content run\dashboard.pid                # dashboard PID
Get-Content run\bot.meta.json                # full bot run metadata

# By command line / port (works even if a pidfile is missing):
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*bot.py*--strategy*' } |
  Select-Object ProcessId, CommandLine
Get-NetTCPConnection -LocalPort 8004 -State Listen | Select-Object OwningProcess
```

**Health model:**
- **Bot** = `run/bot.pid` process alive **AND** `run/bot.heartbeat` refreshed within
  ~2 loop intervals (alive-but-stale = "hung" → manager replaces it).
- **Dashboard** = HTTP probe of `http://localhost:8004` succeeds.

Note: each bot shows as **two** python processes — a `.venv\Scripts\python.exe`
launcher shim and its uv-managed child. The child (in `run/bot.pid`) is the real
loop. That pair is one instance, not a duplicate.

## Dashboard

**Port 8004.** Local: http://localhost:8004 — LAN: http://192.168.0.191:8004

Routes:
- `/`               — Home tab (KPIs, open positions, recent trades, backtest results)
- `/`               — Strategies tab (all 6 strategy cards with entry rules and 3-year P&L)
- `/backtest-2024`  — Full Plotly report for 2024
- `/backtest-2025`  — Full Plotly report for 2025
- `/backtest-2026`  — Full Plotly report for 2026

Run backtests before opening the dashboard or the DB will be empty.

## Bot strategies

One strategy per process. Paper trading only (`paper=True` is hardcoded in `bot.py`).
Strategies: `trend_pullback` (default), `ensemble` (recommended), `regime`,
`breakout`, `momentum_macd`, `mean_reversion`. Pass with `-Strategy <name>`.

**Note on high-priced stocks:** with `dollars_per_trade = $200`, any stock priced
over $200 (e.g. ARM) computes `qty = 0`. The bot now **skips** these entirely
(no order, no email) because a bracket SL/TP order needs whole shares. This is the
fix for the old "Qty 0" email spam — see `bot.py` around the qty check. To actually
trade those names, raise `dollars_per_trade` in `config.py`.

## RULE: restart after changes

Whenever you change any dashboard file (`dashboard/server.py`, `dashboard/index.html`,
`dashboard/db.py`, `dashboard/bot_hooks.py`) or `bot.py`, you MUST restart via the
manager and confirm it came back up:

1. **Dashboard:** `pwsh scripts\manage.ps1 restart-dashboard`, then post
   `http://localhost:8004` once `status` shows it HEALTHY.
2. **Bot:** `pwsh scripts\manage.ps1 restart-bot -Strategy <strategy>`.
3. Run `pwsh scripts\manage.ps1 status` and confirm HEALTHY in your response.

Do not leave the user without a live server after a dashboard change. Do **not**
start a fresh process if one is already healthy — `restart-*` handles the swap.

## RULE: version, changelog, commit + push + tag on every change

Every change MUST be released through this loop (full details in `AGENTS.md`):

1. **Document** it in `CHANGELOG.md` (under the current version's Added/Fixed/Changed).
2. **Bump** the build version for anything user-visible/behavioural:
   `pwsh scripts\version.ps1 -Bump patch|minor|major` (edits `VERSION` + scaffolds
   the changelog section).
3. **Commit AND push** — never leave work committed-but-unpushed.
4. The `post-commit` hook automatically **tags** the commit
   `v<version>+build<N>-<datetime>` and **pushes** it. Confirm the push landed;
   if it reports a failure, run `git push --follow-tags`.

The candle timeframe for the whole system is **4h** (see `BAR_TIMEFRAME` in
`config.py`, sourced from Alpaca via `data_feed.py`). Backtests are kept as
historical records in the DB (`timeframe` column) and shown under **Backtest
History** on the dashboard Home tab.
