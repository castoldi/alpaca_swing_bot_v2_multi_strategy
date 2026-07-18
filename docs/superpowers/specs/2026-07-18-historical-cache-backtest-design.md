# Historical Market Cache and 2016–Present Backtest Design

## Objective

Download the maximum useful Alpaca equity history once, retain it locally, and
run all seven strategies from 2016 through the latest completed market data.
Subsequent backtests must reuse the local data and request only missing date
ranges from Alpaca.

## Decisions

- Historical backtests use Alpaca SIP bars because SIP supplies consolidated US
  exchange coverage and the configured account can retrieve it back to January
  2016. Live bot bars and snapshots remain on IEX so recent-data subscription
  rules cannot disrupt trading.
- The persistent cache is SQLite rather than CSV or Parquet. SQLite is already
  used by the project, is transactional, supports indexed range reads and
  idempotent upserts, and requires no new dependency. Parquet would add an
  optional `pyarrow` or `fastparquet` dependency, while per-symbol CSV files
  make incremental range merging and atomic updates harder.
- ARM participates from its first available session, September 14, 2023. It is
  absent—not synthesized or backfilled—before its IPO. Older results therefore
  use the four symbols that existed then.
- The primary result is one portfolio-capped, cumulative 2016–present run. The
  runner also emits yearly strategy summaries derived from the accepted
  cumulative trades so regime changes are visible without changing portfolio
  decisions after the fact.
- The runner ends at the latest available completed data instead of marking
  future dates as cached. Indicator warmup starts 90 calendar days before the
  requested start; the API's January 2016 boundary naturally means the first
  signals appear only after their indicators have enough bars.

## Components

### Alpaca feed interface

`data_feed.fetch_bars` gains explicit `feed` and `strict` keyword arguments.
The defaults remain `feed="iex"` and `strict=False`, preserving every live bot
caller. Historical cache callers use `feed="sip"` and `strict=True`; strict mode
propagates an API failure so a failed request is never recorded as successfully
covered. Returned frames keep the existing normalized OHLCV shape.

### Persistent cache

`market_cache.py` owns `cache/market_data.db`. Its public operation accepts a
symbol, timeframe, start, end, feed, and injectable fetch function, and returns
a normalized DataFrame for that interval.

The `bars` table is keyed by `(symbol, timeframe, feed, adjustment, timestamp)`
and stores open, high, low, close, and volume. The `coverage` table records the
successfully requested lower and upper boundaries for the same series key. A
cache miss downloads the full requested interval. A wider later request fetches
only the missing prefix or suffix, upserts bars transactionally, advances
coverage only after success, and then reads the requested range from SQLite.
Empty successful ranges count as covered, which prevents repeated pre-IPO ARM
requests. Failed requests leave both bars and coverage unchanged.

The cache end is clamped to the current UTC time. Rows are sorted, duplicate
timestamps are rejected by the primary key, and SQL parameters are used for all
values. The `cache/` directory is ignored by Git; downloaded market data and
generated reports remain local artifacts.

### Backtest data access

`backtest_2025.download_history` becomes a read-through-cache consumer while
retaining its current signature and 90-day warmup behavior. This automatically
benefits the existing 2024, 2025, and 2026 runners. It requests SIP data from the
cache, then applies the existing completed-daily-session guard.

### Historical runner and outputs

`backtest_history.py` defaults to January 1, 2016 through today and supports
optional `--start`, `--end`, and `--strategy` arguments. It loads each required
`(ticker, timeframe)` series once, runs the registered strategies with the
existing signal, exit, position sizing, and portfolio-cap engines, and includes
only data available for each ticker.

It writes:

- `reports/backtest_2016_present.html`, an interactive cumulative report using a
  parameterized report label and accurate Alpaca SIP source text;
- `reports/backtest_2016_present.json`, containing the requested/actual date
  range, cache metadata, cumulative strategy metrics, and yearly metrics.

The console prints a compact strategy comparison and yearly table. Existing
annual dashboard database records and dashboard routes remain unchanged; this
keeps the feature independent of the live dashboard and avoids representing a
multi-year result as a single calendar year.

## Error Handling

- Alpaca download failures abort the historical run with the symbol, timeframe,
  feed, and requested interval in the error rather than silently producing a
  partial result.
- A genuinely empty series logs a warning and is skipped. ARM before its IPO is
  expected and does not fail the run once later bars exist.
- SQLite writes occur in transactions. An exception rolls back both bar inserts
  and coverage changes.
- Invalid date ranges or dates before 2016 are rejected by the historical runner
  with a clear command-line error.

## Verification

Unit tests use a temporary SQLite file and an injected fake downloader. They
cover initial population, cache hits with zero network calls, prefix/suffix
incremental downloads, deduplication, empty successful intervals, failure
rollback, feed selection, and future-end clamping. Runner tests cover partial
symbol histories, cumulative portfolio capping, yearly aggregation, date
validation, and report labeling.

The final integration verification downloads SIP daily and 4-hour bars for the
five configured tickers, reruns from the populated cache to prove no redundant
downloads, executes all seven strategies from 2016 to the latest data, validates
the JSON/HTML artifacts, runs the complete pytest suite, and confirms the bot
and dashboard singleton status was not changed.

