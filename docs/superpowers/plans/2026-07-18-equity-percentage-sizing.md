# Equity-Percentage Position Sizing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Size live and annual-backtest positions at no more than 20% of equity, compound realized P&L within each year, and reset every reported year to a fresh $1,000.

**Architecture:** Put whole-share sizing in a small shared module. Replace the fixed-dollar backtest cap with chronological annual portfolio events built from quantity-independent entry candidates and both live-compatible exit paths. Live trading reads one account snapshot per cycle, uses current snapshot prices, and reserves cash locally after each submitted order.

**Tech Stack:** Python 3, dataclasses, pandas, alpaca-py, FastAPI, vanilla JavaScript, pytest, PowerShell release/process scripts.

## Global Constraints

- Position size is exactly 20% of valid equity, capped by available non-margin cash.
- Live entries retain their existing protected market/OTO/bracket order types.
- All quantities are positive whole shares; an unaffordable ticker is skipped.
- Every annual simulation starts at `$1,000` and compounds only P&L realized inside that year.
- Historical reports reset to `$1,000` at each calendar-year boundary.
- At most five portfolio positions may be open, and all scale-out legs consume one slot.
- One- and two-share bracket positions use a single TP3 bracket; quantities of three or more use the three-target scale-out lifecycle.
- Existing unrelated historical-cache work in the working tree must be preserved and checkpointed separately before editing overlapping files.
- Live services are restarted only with `scripts/manage.ps1` and must remain singletons.
- All production behavior follows failing-test-first TDD.
- Update `CHANGELOG.md`, bump the minor version, commit, tag, and push.

---

### Task 0: Verify and checkpoint the pre-existing historical-cache implementation

**Files:**
- Existing modified: `.gitignore`, `backtest_2025.py`, `build_report_2025.py`, `data_feed.py`, `logger_setup.py`, `tests/test_backtest_timeframes.py`, `tests/test_data_feed_timeframes.py`
- Existing untracked: `backtest_history.py`, `market_cache.py`, `tests/test_backtest_history.py`, `tests/test_logger_setup.py`, `tests/test_market_cache.py`
- Modify for release: `CHANGELOG.md`, `VERSION`

**Interfaces:**
- Preserves the already written SIP cache and historical runner described by `docs/superpowers/plans/2026-07-18-historical-cache-backtest.md`.
- Produces a clean main worktree before percentage-sizing implementation touches the same runners.

- [ ] **Step 1: Run the prior feature's focused verification**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_market_cache.py tests/test_data_feed_timeframes.py tests/test_backtest_history.py tests/test_logger_setup.py tests/test_backtest_timeframes.py -q
```

Expected: all selected tests pass. If a test fails, follow `superpowers:systematic-debugging` and correct only the historical-cache implementation before proceeding.

- [ ] **Step 2: Run the full regression suite**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q
```

Expected: zero failures.

- [ ] **Step 3: Bump and document the historical-cache release**

Run:

```powershell
pwsh scripts\version.ps1 -Bump minor
```

Fill the generated changelog section with the existing cache, strict SIP retrieval, per-process logging, and 2016–present runner bullets. Do not stage percentage-sizing work in this checkpoint.

- [ ] **Step 4: Commit and verify the automatic push**

Run:

```powershell
git add -- .gitignore CHANGELOG.md VERSION backtest_2025.py backtest_history.py build_report_2025.py data_feed.py logger_setup.py market_cache.py tests/test_backtest_history.py tests/test_backtest_timeframes.py tests/test_data_feed_timeframes.py tests/test_logger_setup.py tests/test_market_cache.py
git diff --cached --check
git commit -m "feat: cache historical SIP market data"
git status --short
git log -1 --oneline --decorate
```

Expected: the hook creates and pushes a versioned build tag, and the listed prior-feature files are clean.

---

### Task 1: Add the shared whole-share sizing policy

**Files:**
- Create: `position_sizing.py`
- Create: `tests/test_position_sizing.py`
- Modify: `config.py:59-64`

**Interfaces:**
- Produces `PositionSize(quantity: int, budget: float, notional: float, reason: str | None)`.
- Produces `whole_share_position_size(equity: float, cash: float, price: float, fraction: float) -> PositionSize`.
- Produces `StrategyParams.initial_backtest_equity`, `StrategyParams.position_size_pct`, and `StrategyParams.max_concurrent_positions`.

- [ ] **Step 1: Write failing sizing tests**

Create `tests/test_position_sizing.py` with:

```python
import math

from position_sizing import whole_share_position_size


def test_sizes_twenty_percent_of_equity_in_whole_shares():
    result = whole_share_position_size(1_000.0, 1_000.0, 60.0, 0.20)
    assert result.quantity == 3
    assert result.budget == 200.0
    assert result.notional == 180.0
    assert result.reason is None


def test_caps_order_by_available_cash():
    result = whole_share_position_size(2_000.0, 150.0, 60.0, 0.20)
    assert result.quantity == 2
    assert result.budget == 150.0
    assert result.notional == 120.0


def test_skips_price_above_allocation():
    result = whole_share_position_size(1_000.0, 1_000.0, 250.0, 0.20)
    assert result.quantity == 0
    assert result.reason == "budget_below_one_share"


def test_rejects_invalid_numeric_inputs():
    cases = [
        (math.nan, 1_000.0, 100.0, 0.20),
        (1_000.0, -1.0, 100.0, 0.20),
        (1_000.0, 1_000.0, 0.0, 0.20),
        (1_000.0, 1_000.0, 100.0, 0.0),
        (1_000.0, 1_000.0, 100.0, 1.01),
    ]
    for args in cases:
        result = whole_share_position_size(*args)
        assert result.quantity == 0
        assert result.reason == "invalid_input"
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_position_sizing.py -q
```

Expected: collection fails because `position_sizing` does not exist.

- [ ] **Step 3: Implement the minimal sizing module and configuration**

Create `position_sizing.py` with a frozen `PositionSize` dataclass. Implement validation with `math.isfinite`, require `equity > 0`, `cash >= 0`, `price > 0`, and `0 < fraction <= 1`, calculate `budget = min(equity * fraction, cash)`, and calculate `quantity = math.floor(budget / price)`. Return `invalid_input` or `budget_below_one_share` without raising.

Replace the two fixed-dollar fields in `StrategyParams` with:

```python
initial_backtest_equity: float = 1000.0
position_size_pct: float = 0.20
max_concurrent_positions: int = 5
```

- [ ] **Step 4: Verify GREEN and run configuration smoke tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_position_sizing.py tests/test_smoke.py -q
```

Expected: all selected tests pass.

---

### Task 2: Build quantity-independent candidates and live-compatible exits

**Files:**
- Create: `backtest_portfolio.py`
- Create: `tests/test_backtest_portfolio.py`
- Modify: `strategies/base.py:152-460`
- Modify: `strategies/__init__.py`
- Modify: `strategy.py`

**Interfaces:**
- Produces `BacktestCandidate(ticker, entry_date, entry_price, stop_loss, take_profit, strategy, single_legs, scaled_legs)`.
- Produces `collect_backtest_candidates(frame, ticker, window_start, window_end, params, strategy) -> list[BacktestCandidate]`.
- Produces `materialize_candidate(candidate, quantity) -> list[Trade]`.

- [ ] **Step 1: Write failing candidate and exit-mode tests**

Add tests that construct a candidate with one single-bracket `ExitLeg` at TP3 and three scaled `ExitLeg` objects. Assert:

```python
assert [t.shares for t in materialize_candidate(candidate, 2)] == [2]
assert [t.exit_reason for t in materialize_candidate(candidate, 2)] == ["take_profit"]
assert [t.shares for t in materialize_candidate(candidate, 7)] == [2, 2, 3]
assert [t.exit_reason for t in materialize_candidate(candidate, 7)] == ["tp1", "tp2", "tp3"]
```

Add a repeated-signal frame test proving candidate collection does not suppress later signals before the portfolio layer decides whether the ticker is open. Add an annual-end test proving all simulated exits are clipped to the last completed bar on or before `window_end`.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_backtest_portfolio.py -q
```

Expected: imports fail because the candidate APIs do not exist.

- [ ] **Step 3: Implement candidate collection**

Move quantity-independent signal discovery into `collect_backtest_candidates`:

- add indicators and the existing earnings filter once per ticker/year;
- accept signal timestamps from `window_start` through `window_end`;
- apply the existing TP reachability filter to bracket strategies;
- precompute a one-leg result with `simulate_exit` and a scale-out result with `simulate_exit_scaleout` on a frame clipped at annual end;
- preserve next-session entry, gap-stop, emergency-stop, and cross-down behavior for `signal_with_stop` strategies;
- emit all valid candidate signals and leave one-position-per-ticker enforcement to the portfolio engine.

- [ ] **Step 4: Implement integer-share materialization**

For quantities below three, use the candidate's single legs and assign the full integer quantity. For quantities of three or more, use `split_qty(quantity)` for TP1/TP2/TP3 and assign every non-TP terminal leg all remaining shares. Create `Trade` values with integer-valued `shares`, exact dollar P&L, and unchanged percentage P&L.

- [ ] **Step 5: Export the candidate API**

Export the new candidate and materialization APIs through
`strategies/__init__.py` and `strategy.py`. Leave the existing
`backtest_ticker` wrapper in place until the annual engine exists in Task 3.

- [ ] **Step 6: Verify candidate and existing exit tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_backtest_portfolio.py tests/test_simulate_exit_scaleout.py tests/test_sma_cross_backtest.py -q
```

Expected: all selected tests pass.

---

### Task 3: Add the annual realized-equity portfolio ledger

**Files:**
- Modify: `backtest_portfolio.py`
- Modify: `tests/test_backtest_portfolio.py`
- Modify: `tests/test_compute_stats.py`
- Modify: `backtest_2025.py`
- Modify: `strategies/base.py`
- Modify: `strategy.py`

**Interfaces:**
- Produces `PortfolioResult(trades, starting_equity, ending_equity, return_pct, accepted_positions, skipped_positions, equity_curve)`.
- Produces `run_annual_portfolio(candidates, *, initial_equity, position_fraction, max_positions) -> PortfolioResult`.

- [ ] **Step 1: Write failing compounding and capacity tests**

Use deterministic candidates with integer-friendly prices to prove:

- a first profitable exit raises `starting_equity + realized_pnl` enough for a later order to buy an additional whole share;
- a first loss lowers a later order by at least one whole share;
- five overlapping candidates are accepted and the sixth is skipped;
- one three-leg position consumes one slot, not three;
- available cash caps entries even when fewer than five slots are open;
- a second candidate for an open ticker is skipped;
- ending equity equals `$1,000 + sum(trade.pnl_dollars)` after all exits.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_backtest_portfolio.py -q
```

Expected: failures for the missing portfolio result and runner.

- [ ] **Step 3: Implement the chronological ledger**

Sort candidates by `(entry_date, ticker)`. Before every entry, realize pending exit legs at or before the entry timestamp, return sale proceeds to cash, add P&L to realized equity, and release the ticker/position slot only when its final share exits. Use `whole_share_position_size(initial_equity + realized_pnl, cash, entry_price, position_fraction)` for every accepted entry. Deduct reference-price cost immediately and process all remaining exits after the last candidate.

Store the equity curve as `(exit_timestamp, initial_equity + realized_pnl)` points. `return_pct` is `(ending_equity - starting_equity) / starting_equity`.

- [ ] **Step 4: Replace drawdown arithmetic with initial-equity-aware drawdown**

Update `compute_max_drawdown` to begin at the annual `$1,000`, process realized P&L by exit timestamp, and divide each drawdown by its running equity peak. Add a test where a `$100` gain followed by a `$55` loss produces a 5% drawdown from `$1,100`.

- [ ] **Step 5: Preserve the compatibility API**

Reimplement `backtest_ticker` as a compatibility wrapper that collects
candidates for the requested window and runs a single annual portfolio with
the configured initial equity, fraction, and position limit. Preserve its
existing arguments and `list[Trade]` return type.

- [ ] **Step 6: Verify GREEN**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_backtest_portfolio.py tests/test_compute_stats.py -q
```

Expected: all selected tests pass.

---

### Task 4: Integrate annual reset compounding into all runners and research

**Files:**
- Modify: `backtest_2024.py`
- Modify: `backtest_2025.py`
- Modify: `backtest_2026.py`
- Modify: `backtest_history.py`
- Modify: `research/optimizer.py`
- Modify: `tests/test_backtest_history.py`
- Modify: `tests/test_backtest_timeframes.py`

**Interfaces:**
- Produces `run_strategy_year(ticker_data, strategy, year, params=PARAMS) -> tuple[PortfolioResult, dict]` in `backtest_2025.py` for reuse by annual runners.
- Historical JSON adds `annual_reset: true`, `initial_equity: 1000.0`, and annual `starting_equity`, `ending_equity`, `return_pct` fields.

- [ ] **Step 1: Write failing annual-reset runner tests**

Update `tests/test_backtest_history.py` so two synthetic years each contain the same first candidate. Assert both yearly outputs begin at `$1,000` and use the same first-order quantity even when year one ends profitably. Assert the cumulative payload marks `annual_reset` true and sums annual P&L without carrying ending equity into the next year.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_backtest_history.py tests/test_backtest_timeframes.py -q
```

Expected: failures for missing annual portfolio metadata and reset behavior.

- [ ] **Step 3: Add the shared annual strategy runner**

In `backtest_2025.py`, collect candidates for each ticker, call `run_annual_portfolio` once for the combined strategy portfolio, calculate stats from accepted trades, attach portfolio metadata, and build per-ticker detail from accepted trades only. Replace every `apply_portfolio_cap` call in the 2024, 2025, and 2026 full/single runners with this shared path.

- [ ] **Step 4: Reset the historical runner by calendar year**

For every strategy and every requested calendar year, clip the year to the requested range, collect that year's candidates, and call a new `$1,000` annual portfolio. Store annual results directly rather than grouping one continuous accepted-trade list by exit year. Aggregate cumulative trade statistics and summed P&L, but store `annual_reset = True` and never expose the sum as continuously compounded ending equity.

- [ ] **Step 5: Update the optimizer**

Remove `dollars_per_trade` from the mutation bounds and `StrategyParams` construction. Run candidate collection and the same annual portfolio engine for each parameter set so optimizer rankings reflect 20% whole-share compounding.

- [ ] **Step 6: Verify runner integration**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_backtest_history.py tests/test_backtest_timeframes.py tests/test_backtest_ticker_scaleout.py tests/test_sma_cross_backtest.py -q
```

Expected: all selected tests pass.

---

### Task 5: Apply 20%-of-equity sizing to live entries

**Files:**
- Modify: `bot.py:221-360`
- Create: `tests/test_bot_position_sizing.py`
- Modify: `tests/test_bot_duplicate_entry_guards.py`
- Modify: `tests/test_sma_cross_bot.py`

**Interfaces:**
- Produces `LiveSizingState(equity: float, remaining_cash: float)`.
- Produces `_load_live_sizing(trading_client) -> LiveSizingState | None`.
- Consumes `whole_share_position_size` for every new live entry.

- [ ] **Step 1: Write failing live-cycle tests**

Use fake Alpaca account values and broker methods to prove:

- `$1,000` equity and cash plus a `$60` snapshot price submits three shares;
- `$2,000` equity with only `$150` cash submits two `$60` shares and does not use margin;
- two signals in one cycle decrement local cash before sizing the second order;
- a `$250` price on `$1,000` equity submits nothing and sends no notification;
- account retrieval failure submits no new entries but still calls `_reconcile_and_exit`;
- every strategy uses `data_feed.fetch_snapshots` for sizing instead of its completed-bar signal price.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_bot_position_sizing.py -q
```

Expected: failures because live sizing state and percentage sizing do not exist.

- [ ] **Step 3: Implement one account snapshot per cycle**

Obtain `_get_trading()` and `get_account()` once before the ticker loop. Parse finite positive `equity` and non-negative `cash`; otherwise log one entry-disabled warning and retain `None`. Do not return early from `run_once` because open-position reconciliation must still execute.

- [ ] **Step 4: Size and reserve every eligible order**

Fetch a current snapshot for all strategies immediately before order construction. Pass equity, local remaining cash, live price, and `PARAMS.position_size_pct` to `whole_share_position_size`. Log and skip quantity zero. After a successful submit, subtract `result.notional` from remaining cash. Preserve stop-only, scaled, and single-bracket order construction and all duplicate-entry ownership guards.

- [ ] **Step 5: Verify live sizing and bot regressions**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_bot_position_sizing.py tests/test_bot_duplicate_entry_guards.py tests/test_sma_cross_bot.py tests/test_place_scaled_entry.py -q
```

Expected: all selected tests pass.

---

### Task 6: Update reports, dashboard metadata, and project documentation

**Files:**
- Modify: `build_report_2025.py`
- Modify: `dashboard/server.py`
- Modify: `dashboard/index.html`
- Modify: `tests/test_dashboard_sma_cross.py`
- Modify: `README.md`
- Modify: `program.md`
- Modify: `AGENTS.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Dashboard `/api/summary` returns `position_size_pct`, `initial_backtest_equity`, and `max_concurrent_positions`.
- Report stats display starting equity, ending equity, return percentage, and dollar P&L.

- [ ] **Step 1: Write failing metadata/report tests**

Assert the summary response exposes 0.20, 1000.0, and 5; assert Home metadata contains `20% equity/trade`; assert generated report HTML contains `Starting Equity`, `Ending Equity`, and `Annual reset: $1,000` and no `$200/trade` label.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_dashboard_sma_cross.py tests/test_backtest_timeframes.py -q
```

Expected: assertions fail on fixed-dollar metadata.

- [ ] **Step 3: Update report and dashboard presentation**

Replace `dollars_per_trade` and `max_capital` response fields with the percentage, initial equity, and position-count fields. Render `20% equity/trade · 5 positions max` on Home. Replace report parameter strings with `20% equity/trade`, include starting/ending equity and return in strategy cards, and start equity curves at `$1,000` rather than zero cumulative P&L.

- [ ] **Step 4: Update documentation and changelog**

Replace every active claim of `$200/trade` and `$1,000 max concurrent capital` with the live 20% equity rule and five-position maximum. Document that each annual backtest compounds realized P&L but resets to `$1,000` for the next year. Preserve historical changelog entries describing older fixed-dollar behavior.

- [ ] **Step 5: Verify metadata and docs**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_dashboard_sma_cross.py tests/test_backtest_timeframes.py -q
rg -n "\$200/trade|dollars_per_trade|max_concurrent_capital" README.md program.md AGENTS.md config.py bot.py backtest_2024.py backtest_2025.py backtest_2026.py backtest_history.py build_report_2025.py dashboard research strategies
```

Expected: tests pass; search results exist only in preserved historical changelog text or intentional compatibility notes.

---

### Task 7: Full verification, regenerated results, release, and managed restarts

**Files:**
- Modify: `VERSION`
- Modify: `CHANGELOG.md`
- Generated local artifacts: `reports/*`, `dashboard/swing_bot_v2.db`

**Interfaces:**
- Produces verified 2024, 2025, 2026, and 2016–present percentage-sized results.
- Produces a healthy singleton dashboard and ensemble bot.

- [ ] **Step 1: Run the full automated suite**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q
```

Expected: zero failures and no unexpected warnings.

- [ ] **Step 2: Regenerate annual and historical backtests**

Run:

```powershell
.venv\Scripts\python.exe backtest_2024.py
.venv\Scripts\python.exe backtest_2025.py
.venv\Scripts\python.exe backtest_2026.py
.venv\Scripts\python.exe backtest_history.py
```

Expected: all commands exit zero; each annual strategy starts at `$1,000`, reports integer shares, and records ending equity/return. The historical JSON marks annual reset and every year starts at `$1,000`.

- [ ] **Step 3: Run fresh verification after generated data**

Run:

```powershell
.venv\Scripts\python.exe -m pytest -q
git diff --check
```

Expected: zero test failures and no whitespace errors.

- [ ] **Step 4: Bump the user-visible release and commit**

Run:

```powershell
pwsh scripts\version.ps1 -Bump minor
git add -- AGENTS.md CHANGELOG.md VERSION README.md program.md config.py position_sizing.py backtest_portfolio.py bot.py strategies/base.py strategies/__init__.py strategy.py backtest_2024.py backtest_2025.py backtest_2026.py backtest_history.py build_report_2025.py research/optimizer.py dashboard/server.py dashboard/index.html tests
git diff --cached --check
git commit -m "feat: compound positions at twenty percent equity"
```

Expected: the post-commit hook tags and pushes the commit to `origin/main`.

- [ ] **Step 5: Restart through the singleton manager**

Run:

```powershell
pwsh scripts\manage.ps1 restart-dashboard
pwsh scripts\manage.ps1 restart-bot -Strategy ensemble
pwsh scripts\manage.ps1 status
```

Expected: BOT and DASHBOARD both report `HEALTHY`, with exactly one managed instance of each service.

- [ ] **Step 6: Verify the push and dashboard endpoint**

Run:

```powershell
git status --short
git log -1 --oneline --decorate
git ls-remote --heads origin main
Invoke-WebRequest -UseBasicParsing http://localhost:8004/api/summary | Select-Object -ExpandProperty Content
```

Expected: only intentional generated/ignored files remain, remote main contains the new commit, and the summary JSON reports `position_size_pct: 0.2`, `initial_backtest_equity: 1000.0`, and `max_concurrent_positions: 5`.
