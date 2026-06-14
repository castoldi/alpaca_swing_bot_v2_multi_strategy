# Alpaca Swing Bot V2 — Agent Instructions

## What's new in V2

V2 is an **autoresearch-inspired** swing trading bot with autonomous strategy experimentation. Built on the foundation of V1 but with:

- **6 strategies** (3 V1 + 3 V2): Trend Pullback, Breakout, Mean Reversion, **MACD Momentum**, **Ensemble** (weighted vote), **Regime Adaptive**
- **Autonomous research loop** via `program.md` — agents can propose, backtest, evaluate, and log experiments
- **SQLite database** tracking: trades, signals, bot runs, backtest runs, and research experiments
- **Dashboard on port 8004** (`http://localhost:8004`) — multi-tab SPA with Home (live positions, trades, backtest DB), Strategies (all 6 with entry rules + 3-year P&L), and per-year Plotly reports (2024/2025/2026)
- **Research modules**: `research/optimizer.py` (param search), `research/regime_detector.py` (market regime classifier)

## Quick start

```powershell
# Windows — activate venv first
cd C:\Data\ai_projects\alpaca_swing_bot_v2_multi_strategy
.venv\Scripts\activate
pip install -r requirements.txt

# Backtest all 6 strategies (writes to dashboard/swing_bot_v2.db)
python backtest_2024.py
python backtest_2025.py
python backtest_2026.py

# Run backtests first or the dashboard DB will be empty.
```

## Running the bot & dashboard — use the manager (NEVER start duplicates)

The bot and dashboard are **singletons**. Two `--loop` bots running at once flooded
the user with duplicate emails, so a long-running process must be started **only**
through `scripts/manage.ps1`, which checks for a live, healthy instance and refuses
to spawn a duplicate. Do **not** call `python bot.py --loop` or the raw `uvicorn`
command directly to start one.

```powershell
pwsh scripts\manage.ps1 status                       # what's running + health
pwsh scripts\manage.ps1 start-bot                     # ensemble loop @30m (idempotent)
pwsh scripts\manage.ps1 start-bot -Strategy regime -Interval 60
pwsh scripts\manage.ps1 restart-bot                   # after editing bot.py
pwsh scripts\manage.ps1 stop-bot
pwsh scripts\manage.ps1 start-dashboard               # idempotent — http://localhost:8004
pwsh scripts\manage.ps1 restart-dashboard             # after editing dashboard/*
pwsh scripts\manage.ps1 stop-dashboard
```

`start-*` is safe to call repeatedly: a healthy instance is left alone; a dead/hung
one is replaced and any orphan processes are swept first.

Dashboard URL: http://localhost:8004 (LAN: http://192.168.0.191:8004). Routes:
`/` Home (KPIs, positions, trades, backtests), `/` Strategies (6 cards + live P&L),
`/backtest-2024`, `/backtest-2025`, `/backtest-2026` (Plotly reports).

### Finding the PIDs

Runtime state is in `run/` (git-ignored): `bot.pid`, `bot.meta.json`,
`bot.heartbeat` (written by `bot.py` via `runtime.py`); `dashboard.pid`,
`dashboard.meta.json` (written by `manage.ps1`).

```powershell
pwsh scripts\manage.ps1 status     # preferred — PID + HEALTHY/UNHEALTHY/STOPPED
Get-Content run\bot.pid            # bot loop PID    (Get-Content run\dashboard.pid for dashboard)
Get-Content run\bot.meta.json      # strategy, interval, started_at, cmd
# Fallback by command line / port:
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*bot.py*--strategy*' } | Select ProcessId,CommandLine
Get-NetTCPConnection -LocalPort 8004 -State Listen | Select OwningProcess
```

Health = bot pid alive AND `bot.heartbeat` fresh (within ~2 intervals); dashboard =
`http://localhost:8004` responds. Each bot is **two** python processes (a venv
launcher shim + its uv-managed child); the child in `run/bot.pid` is the real loop —
that pair is one instance, not a duplicate.

## Important: one strategy at a time

The bot runs **one strategy per process**. `--strategy` selects which one (default: `trend_pullback`). There is no built-in way to run multiple strategies simultaneously — you would need separate processes. The exception is `ensemble`, which internally polls all 5 base strategies to compute a weighted vote but still runs as a single process and places one set of orders.

## File structure

```
alpaca_swing_bot_v2_multi_strategy/
├── config.py                  # Enhanced config with V2 strategy params
├── strategy.py                # 6 strategies + indicators + backtest engine
├── bot.py                     # Live Alpaca paper trader
├── backtest_2024.py           # 2024 full-year multi-strategy backtest
├── backtest_2025.py           # 2025 full-year multi-strategy backtest
├── backtest_2026.py           # 2026 full-year multi-strategy backtest
├── build_report_2025.py       # Shared Plotly HTML report builder
├── notifier.py                # Gmail SMTP
├── logger_setup.py            # Rotating file logger
├── requirements.txt           # Dependencies (v2 adds scipy, sklearn)
├── .env                       # API keys (same as V1)
├── program.md                 # Autoresearch-style research program
├── AGENTS.md                  # ← This file (agent instructions)
├── README.md                  # Project docs
│
├── dashboard/
│   ├── server.py              # FastAPI on port 8004
│   ├── index.html             # Dark-theme dashboard SPA
│   ├── db.py                  # SQLite CRUD + position sync
│   ├── bot_hooks.py           # Bridge db ↔ bot
│   └── swing_bot_v2.db        # SQLite database (auto-created)
│
├── research/
│   ├── optimizer.py           # Random search parameter optimizer
│   └── regime_detector.py     # Market regime classifier
│
├── reports/                   # Backtest HTML reports
├── logs/                      # Rotating logs
├── run/                       # PID/heartbeat state (git-ignored)
├── VERSION                    # Canonical semantic version (manually bumped)
├── CHANGELOG.md               # Keep a Changelog history, keyed by version
├── scripts/
│   ├── manage.ps1             # Singleton bot/dashboard controller
│   ├── version.ps1            # Show/bump version, list builds
│   └── git-hooks/post-commit  # Auto-tags every commit
└── .venv/                     # Python venv
```

## Versioning, changelog & release workflow — REQUIRED for every change

**Every change to this project MUST follow this loop. No exceptions.**

1. **Record it in `CHANGELOG.md`.** Add bullet(s) under the current version's
   `### Added/Fixed/Changed`. Nothing ships undocumented.
2. **Bump the build version** when the change is user-visible or behavioural:
   `pwsh scripts\version.ps1 -Bump patch|minor|major` (updates `VERSION` and
   scaffolds a dated `CHANGELOG.md` section to fill in). The semantic version
   lives in `VERSION`; the full history lives in `CHANGELOG.md`.
3. **Always commit AND push.** Never leave work committed-but-unpushed.
4. **Tag with the version + datetime** — this is automatic: the `post-commit`
   hook tags every commit `v<version>+build<N>-<datetime>` (`N` = total commit
   count) **and pushes the commit + tag** to the upstream branch (best-effort).

So in practice: update `CHANGELOG.md` → bump `VERSION` if needed → `git commit`.
The hook does the tag + push. Verify the push landed; if the hook reports a push
failure, run `git push --follow-tags` manually.

```powershell
pwsh scripts\version.ps1                 # current version, build #, latest tag
pwsh scripts\version.ps1 -Bump minor     # bump VERSION + scaffold CHANGELOG entry
pwsh scripts\version.ps1 -Builds         # list all build tags
git config core.hooksPath scripts/git-hooks   # ONE-TIME install (fresh clones only)
```

The hook is installed via `core.hooksPath`, which is **local** git config — re-run
the one-time install command above after a fresh clone (otherwise no auto tag/push).

## Research loop (autoresearch pattern)

1. **EXPLORE** — Edit `strategy.py` with a new signal or parameter change
2. **BACKTEST** — Run `python backtest_2025.py` and `python backtest_2026.py`
3. **EVALUATE** — Compare results in the dashboard or DB
4. **KEEP or REVERT** — If both years improve, keep the change
5. **LOG** — Call `db_mod.log_experiment(...)` or add to `research/experiments.md`
6. **REPEAT**

## All 6 strategies — entry rules and performance

| Strategy | Entry conditions | SL | TP | Max hold | 2025 P&L | 2026 P&L |
|----------|-----------------|----|----|----------|----------|----------|
| **trend_pullback** | Price > SMA(50), RSI dipped below 55 recently, bounce bar (close > open, rising RSI). Skips entries 3 days before earnings. | 10% | 2×ATR [3%, 8%] | 5d | +$71.37 | +$142.02 |
| **breakout** | Price breaks 20-bar high with ≥1.5× avg volume, price > SMA(50), RSI > 50 rising | 8% | 3×ATR [5%, 15%] | 7d | -$26.51 | +$117.66 |
| **mean_reversion** | Price > SMA(50) but below SMA(20), RSI < 50, near Bollinger lower band, bounce bar | 7% | 1.5×ATR [1.5%, 5%] | 3d | -$26.31 | +$26.68 |
| **momentum_macd** | MACD histogram just crossed above 0, RSI > 50 rising, price > SMA(20) and SMA(50) | 9% | 2.5×ATR [4%, 12%] | 6d | +$13.31 | +$35.99 |
| **ensemble** | Weighted vote ≥ 0.30: regime(0.35) + MACD(0.25) + trend(0.20) + breakout(0.15) + MR(0.05) | 9% | 2.5×ATR [4%, 12%] | 6d | +$199.92 | +$331.78 |
| **regime** | EMA(10)/EMA(50) cross: risk-on = buy dips in uptrend, risk-off = oversold bounces only, neutral = trend-pullback-like | adaptive | ATR-based [3%, 8%] | 5d | +$85.69 | +$239.86 |

All strategies share: TP reachability filter (must be reachable in ≤2 ATR-days), one position per ticker, $200/trade, $1,000 max concurrent capital.

## Research ideas — status

- [x] ML signal combiner (weighted ensemble) — ✅ done, ensemble weights tuned 2026-05-28
- [x] Tighter ensemble threshold 0.25→0.30 — ✅ done 2026-05-28
- [x] Earnings-date avoidance filter — ✅ done 2026-06-02 (Trend Pullback only, +$65.22 combined)
- [x] Market regime VIX filter — ❌ reverted (worsened both years on this universe)
- [ ] Adaptive position sizing (Kelly criterion)
- [ ] Multi-timeframe confirmation (1h + daily)
- [ ] Sector rotation overlay
- [ ] Correlation-based drawdown protection

*Full experiment log with P&L impact in `program.md`.*

## RULE: restart after changes

**Whenever you make changes to any dashboard file (`dashboard/server.py`, `dashboard/index.html`, `dashboard/db.py`, `dashboard/bot_hooks.py`) or to `bot.py`, you MUST restart via the manager (it swaps in place — never start a second process):**

1. For the **dashboard**: `pwsh scripts\manage.ps1 restart-dashboard`, then post `http://localhost:8004` once `status` shows it HEALTHY.
2. For the **bot**: `pwsh scripts\manage.ps1 restart-bot -Strategy <strategy>`.
3. Run `pwsh scripts\manage.ps1 status` and confirm HEALTHY in your response.

## Pitfalls

- **Windows paths**: activate venv with `.venv\Scripts\activate`, not `source .venv/Scripts/activate`
- **Alpaca paper only**: `paper=True` is hardcoded in `bot.py` regardless of `.env` — this is intentional
- **Ensemble threshold**: currently 0.30 (tightened from 0.25 on 2026-05-28) — check `strategy.py:467` if tuning
- **Ensemble warmup**: requires 60+ bars before first signal; regime needs 50+ bars for EMA(50)
- **OCO brackets / high-priced stocks**: Alpaca requires whole shares for bracket (SL/TP) orders. With `dollars_per_trade=$200`, a stock priced >$200 gives `qty=0`, so the bot now **skips it entirely** (no order, no email). This replaced the old notional-buy fallback, which placed unprotected positions and spammed "Qty 0" emails every loop. Raise `dollars_per_trade` in `config.py` to trade those names.
- **Never start duplicates**: start the bot/dashboard only via `scripts/manage.ps1` — two `--loop` bots email the user twice over. PIDs live in `run/`.
- **DB population**: backtests write to `dashboard/swing_bot_v2.db` — run both backtest scripts before opening the dashboard or it will appear empty
- **simulate_exit uses signal prices directly**: SL/TP on the signal object are authoritative — do not recalculate from params in `simulate_exit` (bug fixed 2026-05-31)