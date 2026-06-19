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
`data_feed.fetch_4h` → `strategy.add_indicators` → `strategy.check_entry_*` → signal → `bot._place_scaled_entry` → Alpaca + `db.save_trade` → `bot._reconcile_and_exit` → `db.close_trade`

**Backtest flow:**
`data_feed.fetch_4h` → `strategy.add_indicators` → `strategy.backtest_ticker` (calls `check_entry_*` + `simulate_exit_scaleout` bar-by-bar) → `apply_portfolio_cap` → HTML report + `db.finish_backtest_run`

## Strategy architecture

All 6 strategies live in `strategy.py`. The shared exit engine (`simulate_exit_scaleout`) handles the 3-leg TP ladder and stepped stop for all of them.

| Strategy | Key entry condition | SL | TP | Max hold |
|----------|--------------------|----|-----|---------|
| `trend_pullback` | Price > SMA(50), RSI dipped < 55, bounce bar. Earnings filter (3d before skip). | 10% | 2×ATR [3%–8%] | 5d |
| `breakout` | Price breaks 20-bar high, ≥1.5× avg volume, RSI > 50 rising | 8% | 3×ATR [5%–15%] | 7d |
| `mean_reversion` | Price > SMA(50) but < SMA(20), RSI < 50, near Bollinger lower band, bounce | 7% | 1.5×ATR [1.5%–5%] | 3d |
| `momentum_macd` | MACD hist just crossed above 0, RSI > 50 rising, price > SMA(20) & SMA(50) | 9% | 2.5×ATR [4%–12%] | 6d |
| `regime` | EMA(10)/EMA(50) regime: risk-on = buy dips, risk-off = oversold bounces, neutral = trend-like | adaptive | ATR-based [3%–8%] | 5d |
| `ensemble` | Weighted vote ≥ 0.30 across all 5 base strategies (regime 35%, MACD 25%, trend 20%, breakout 15%, MR 5%) | 9% | 2.5×ATR [4%–12%] | 6d |

`bot.py` entry dispatch: `get_entry_checker(strategy)` returns the matching `check_entry_*` function.

All strategies share: TP reachability filter (target reachable in ≤4 ATR-days), one position per ticker, $200/trade, $1,000 max concurrent capital, 3-leg scaled exit (TP1/TP2/TP3 at 1/3–2/3–full with stepped stop).

**Ensemble warmup**: needs 60+ bars before first signal. Regime needs 50+ for EMA(50).

## Bot trading hours

The loop only calls `run_once` between **08:30–17:00 ET** (America/New_York). Outside that window it heartbeats normally and logs `"Outside trading hours"`. The candle timeframe is **4h** — a 30-min loop interval is well-matched (new signals can only appear at 4h candle closes).

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

- **High-priced stocks**: at $200/trade, a stock priced > $200 gives qty=0. Bot skips entirely (no order, no email). Raise `dollars_per_trade` in `config.py` to trade those names.
- **simulate_exit uses signal prices directly**: SL/TP on the `EntrySignal` object are authoritative — do not recalculate from params inside `simulate_exit_scaleout`.
- **Ensemble threshold**: 0.30 (tightened from 0.25 on 2026-05-28) — in `strategy.py` around `check_entry_ensemble`.
- **Alpaca paper hardcoded**: `paper=True` is set in both `bot.py` and `server.py` regardless of `.env`.
- **Windows venv**: `.venv\Scripts\activate` (not `source .venv/Scripts/activate`).
- **DB empty**: backtests write results to `dashboard/swing_bot_v2.db` — run them before opening the dashboard.

## Research loop

1. Edit `strategy.py` (new signal or param tweak)
2. Run `python backtest_2025.py` and `python backtest_2026.py`
3. Compare in dashboard or DB; keep if both years improve
4. Log via `db_mod.log_experiment(...)` or `program.md`
