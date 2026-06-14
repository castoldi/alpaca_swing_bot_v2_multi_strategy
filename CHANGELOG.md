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




