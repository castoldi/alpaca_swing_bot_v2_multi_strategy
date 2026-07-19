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


