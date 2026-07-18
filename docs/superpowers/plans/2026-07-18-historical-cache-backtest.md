# Historical Market Cache and Backtest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist Alpaca SIP bars locally and produce a cumulative plus yearly all-strategy backtest from January 2016 through the latest completed data.

**Architecture:** Keep live callers on the existing IEX default, add strict and feed-selectable historical fetching, and place a transactional SQLite read-through cache between annual/history backtests and Alpaca. A new range runner reuses the existing strategy and portfolio-cap engines, parameterizes the existing Plotly report label, and emits machine-readable yearly summaries.

**Tech Stack:** Python 3, alpaca-py, pandas, SQLite (`sqlite3`), Plotly, pytest, PowerShell release scripts.

## Global Constraints

- Live bot bars and snapshots remain on IEX.
- Historical backtests use SIP with adjustment `all`.
- Cache keys include symbol, timeframe, feed, adjustment, and timestamp.
- ARM participates only from its first available bar.
- No new runtime dependency is added.
- All production changes follow failing-test-first TDD.
- Downloaded cache and generated reports remain Git-ignored local artifacts.
- Update `CHANGELOG.md`, bump the patch version, commit, tag, and push.

---

### Task 1: Feed-selectable strict Alpaca retrieval

**Files:**
- Modify: `data_feed.py:75-107`
- Modify: `tests/test_data_feed_timeframes.py`

**Interfaces:**
- Produces: `fetch_bars(ticker, start, end, timeframe=BAR_TIMEFRAME, *, feed="iex", strict=False) -> pd.DataFrame`
- Produces: `_feed_options(feed: str) -> dict`

- [ ] **Step 1: Write failing tests for feed selection and strict failures**

```python
def test_feed_options_select_requested_alpaca_feed():
    from alpaca.data.enums import DataFeed
    assert data_feed._feed_options("iex")["feed"] == DataFeed.IEX
    assert data_feed._feed_options("sip")["feed"] == DataFeed.SIP


def test_fetch_bars_strict_mode_propagates_client_failure(monkeypatch):
    class BrokenClient:
        def get_stock_bars(self, _request):
            raise RuntimeError("download failed")

    monkeypatch.setattr(data_feed, "_get_client", lambda: BrokenClient())
    with pytest.raises(RuntimeError, match="download failed"):
        data_feed.fetch_bars(
            "AMD", date(2024, 1, 1), date(2024, 1, 2),
            "4h", feed="sip", strict=True,
        )
```

- [ ] **Step 2: Run the focused tests and verify they fail because the new API does not exist**

Run: `.venv\Scripts\python.exe -m pytest tests/test_data_feed_timeframes.py -q`

Expected: failures for `_feed_options("sip")` and unexpected `feed`/`strict` arguments.

- [ ] **Step 3: Implement the minimal backward-compatible feed API**

```python
def _feed_options(feed: str = "iex") -> dict:
    from alpaca.data.enums import Adjustment, DataFeed
    feeds = {"iex": DataFeed.IEX, "sip": DataFeed.SIP}
    if feed.lower() not in feeds:
        raise ValueError(f"Unsupported stock feed: {feed}")
    return {"feed": feeds[feed.lower()], "adjustment": Adjustment.ALL}


def fetch_bars(
    ticker: str,
    start: Union[date, datetime],
    end: Union[date, datetime],
    timeframe: str = BAR_TIMEFRAME,
    *,
    feed: str = "iex",
    strict: bool = False,
) -> pd.DataFrame:
    try:
        amount, unit_name = _timeframe_parts(timeframe)
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame(amount, getattr(TimeFrameUnit, unit_name)),
            start=_as_dt(start),
            end=_as_dt(end),
            **_feed_options(feed),
        )
        bars = _get_client().get_stock_bars(req)
        return _normalize(bars.df, ticker)
    except Exception as exc:
        if strict:
            raise
        log.warning("fetch_bars(%s, %s, %s) failed: %s", ticker, timeframe, feed, exc)
        return pd.DataFrame(columns=_OHLCV)
```

- [ ] **Step 4: Run the focused tests and the existing data-feed/backtest-timeframe tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_data_feed_timeframes.py tests/test_backtest_timeframes.py -q`

Expected: all pass.

### Task 2: Transactional SQLite market-data cache

**Files:**
- Create: `market_cache.py`
- Create: `tests/test_market_cache.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `MarketDataCache(path: Path = CACHE_DB, fetcher=data_feed.fetch_bars, now_fn: Callable[[], datetime] = utc_now)`
- Produces: `MarketDataCache.get_bars(ticker, start, end, timeframe, *, feed="sip", adjustment="all") -> pd.DataFrame`
- Produces: `MarketDataCache.status() -> list[dict]`

- [ ] **Step 1: Write failing tests for initial population and a cache hit**

```python
def sample_bars(start: str, periods: int) -> pd.DataFrame:
    index = pd.date_range(start, periods=periods, freq="D")
    return pd.DataFrame(
        {
            "open": range(100, 100 + periods),
            "high": range(101, 101 + periods),
            "low": range(99, 99 + periods),
            "close": range(100, 100 + periods),
            "volume": [1_000] * periods,
        },
        index=index,
    )


def empty_bars() -> pd.DataFrame:
    return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def recording_fetcher():
    calls = []
    def fetcher(ticker, start, end, timeframe, **kwargs):
        start_dt = pd.Timestamp(start).to_pydatetime()
        end_dt = pd.Timestamp(end).to_pydatetime()
        calls.append((ticker, start_dt, end_dt, timeframe, kwargs))
        index = pd.date_range(start_dt, end_dt, freq="D", inclusive="left")
        return sample_bars(index[0].isoformat(), len(index)) if len(index) else empty_bars()
    return calls, fetcher


def fixed_cache(tmp_path, fetcher, now=None):
    instant = now or datetime(2020, 2, 1, tzinfo=timezone.utc)
    return MarketDataCache(
        tmp_path / "bars.db", fetcher=fetcher, now_fn=lambda: instant
    )


def test_cache_populates_once_then_serves_without_fetch(tmp_path):
    calls = []
    source = sample_bars("2020-01-02", periods=3)

    def fetcher(ticker, start, end, timeframe, **kwargs):
        calls.append((ticker, start, end, timeframe, kwargs))
        return source

    cache = MarketDataCache(tmp_path / "bars.db", fetcher=fetcher,
                            now_fn=lambda: datetime(2020, 2, 1, tzinfo=timezone.utc))
    first = cache.get_bars("AMD", date(2020, 1, 1), date(2020, 1, 10), "1d")
    second = cache.get_bars("AMD", date(2020, 1, 1), date(2020, 1, 10), "1d")
    assert first.equals(second)
    assert len(calls) == 1
    assert calls[0][4] == {"feed": "sip", "strict": True}
```

- [ ] **Step 2: Run the test and verify import failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_market_cache.py::test_cache_populates_once_then_serves_without_fetch -q`

Expected: FAIL because `market_cache` does not exist.

- [ ] **Step 3: Implement schema creation, full-miss fetch, upsert, coverage update, and range read**

```python
class MarketDataCache:
    def __init__(self, path=CACHE_DB, fetcher=None, now_fn=None):
        self.path = Path(path)
        self.fetcher = fetcher or data_feed.fetch_bars
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def get_bars(self, ticker, start, end, timeframe, *, feed="sip", adjustment="all"):
        start_ts = _timestamp(start)
        end_ts = min(_timestamp(end), _completed_data_ceiling(self.now_fn()))
        if end_ts <= start_ts:
            return pd.DataFrame(columns=OHLCV)
        key = (ticker.upper(), timeframe.lower(), feed.lower(), adjustment.lower())
        coverage = self._coverage(key)
        segments = [(start_ts, end_ts)] if coverage is None else []
        if coverage is not None and start_ts < coverage[0]:
            segments.append((start_ts, coverage[0]))
        if coverage is not None and end_ts > coverage[1]:
            segments.append((coverage[1], end_ts))
        for segment_start, segment_end in segments:
            frame = self.fetcher(
                ticker, segment_start.to_pydatetime(), segment_end.to_pydatetime(),
                timeframe, feed=feed, strict=True,
            )
            self._store_segment(key, segment_start, segment_end, frame)
        return self._read(key, start_ts, end_ts)
```

Create `bars` and `coverage` with composite primary keys. Normalize all
boundaries to tz-naive UTC ISO strings and return lowercase OHLCV columns on a
sorted `DatetimeIndex`.

- [ ] **Step 4: Run the initial cache test and verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_market_cache.py::test_cache_populates_once_then_serves_without_fetch -q`

Expected: PASS.

- [ ] **Step 5: Add failing tests for incremental ranges, deduplication, empty coverage, failure safety, and future clamping**

```python
def test_wider_request_fetches_only_missing_prefix_and_suffix(tmp_path):
    calls, fetcher = recording_fetcher()
    cache = fixed_cache(tmp_path, fetcher)
    cache.get_bars("AMD", date(2020, 1, 5), date(2020, 1, 10), "1d")
    cache.get_bars("AMD", date(2020, 1, 1), date(2020, 1, 15), "1d")
    assert [(c[1].date(), c[2].date()) for c in calls] == [
        (date(2020, 1, 5), date(2020, 1, 10)),
        (date(2020, 1, 1), date(2020, 1, 5)),
        (date(2020, 1, 10), date(2020, 1, 15)),
    ]


def test_overlapping_bars_are_upserted_without_duplicates(tmp_path):
    cache = fixed_cache(tmp_path, lambda *args, **kwargs: sample_bars("2020-01-02", 3))
    out = cache.get_bars("AMD", date(2020, 1, 1), date(2020, 1, 10), "1d")
    assert out.index.is_unique
    assert len(out) == 3


def test_empty_successful_range_is_recorded_as_covered(tmp_path):
    calls = []
    cache = fixed_cache(tmp_path, lambda *args, **kwargs: calls.append(args) or empty_bars())
    cache.get_bars("ARM", date(2016, 1, 1), date(2016, 2, 1), "1d")
    cache.get_bars("ARM", date(2016, 1, 1), date(2016, 2, 1), "1d")
    assert len(calls) == 1


def test_failed_fetch_does_not_advance_coverage(tmp_path):
    def broken(*args, **kwargs):
        raise RuntimeError("network down")
    cache = fixed_cache(tmp_path, broken)
    with pytest.raises(RuntimeError, match="network down"):
        cache.get_bars("AMD", date(2020, 1, 1), date(2020, 1, 10), "1d")
    assert cache.status() == []


def test_future_end_is_clamped_to_completed_data_ceiling(tmp_path):
    calls, fetcher = recording_fetcher()
    cache = fixed_cache(tmp_path, fetcher, now=datetime(2020, 1, 10, 18, tzinfo=timezone.utc))
    cache.get_bars("AMD", date(2020, 1, 1), date(2021, 1, 1), "1d")
    assert calls[0][2] == datetime(2020, 1, 10)
```

Assertions must inspect fetch call boundaries, returned index uniqueness,
coverage rows, unchanged state after a raised exception, and the clamped end.

- [ ] **Step 6: Run the new tests and verify each fails for missing behavior**

Run: `.venv\Scripts\python.exe -m pytest tests/test_market_cache.py -q`

Expected: the five new tests fail.

- [ ] **Step 7: Implement prefix/suffix fetches and status metadata**

For an existing coverage row, fetch `[requested_start, covered_start]` only when
the start moves earlier and `[covered_end, requested_end]` only when the end
moves later. Commit bars and the matching boundary in one transaction per
successful segment. Let strict downloader exceptions propagate. `status()`
returns series keys, requested boundaries, row count, first bar, and last bar.

- [ ] **Step 8: Run all cache tests and add `cache/` to `.gitignore`**

Run: `.venv\Scripts\python.exe -m pytest tests/test_market_cache.py -q`

Expected: all pass.

### Task 3: Make every backtest use the persistent SIP cache

**Files:**
- Modify: `backtest_2025.py:51-60`
- Modify: `tests/test_backtest_timeframes.py`

**Interfaces:**
- Consumes: `MarketDataCache.get_bars(ticker, start, end, timeframe, feed="sip")`
- Preserves: `download_history(ticker, start, end, timeframe=BAR_TIMEFRAME) -> pd.DataFrame`

- [ ] **Step 1: Replace the old monkeypatch test with a failing cache-delegation test**

```python
def test_backtest_history_uses_sip_cache_and_drops_current_daily(monkeypatch):
    calls = []
    class FakeCache:
        def get_bars(self, *args, **kwargs):
            calls.append((args, kwargs))
            return bars_with_current_daily_session()
    monkeypatch.setattr(backtest_2025, "_MARKET_CACHE", FakeCache())
    result = backtest_2025.download_history(
        "AMD", date(2026, 1, 1), date(2026, 12, 31), timeframe="1d"
    )
    assert calls[0][1]["feed"] == "sip"
    assert current_session_date not in result.index.date
```

- [ ] **Step 2: Run the test and verify `_MARKET_CACHE` is missing**

Run: `.venv\Scripts\python.exe -m pytest tests/test_backtest_timeframes.py -q`

Expected: FAIL because backtests do not yet expose/use `_MARKET_CACHE`.

- [ ] **Step 3: Instantiate the cache once and delegate warmup retrieval**

```python
from market_cache import MarketDataCache
_MARKET_CACHE = MarketDataCache()

bars = _MARKET_CACHE.get_bars(
    ticker, warmup_start, end + timedelta(days=1), timeframe, feed="sip"
)
```

- [ ] **Step 4: Run focused tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_backtest_timeframes.py tests/test_market_cache.py -q`

Expected: all pass.

### Task 4: Parameterize the existing HTML report

**Files:**
- Modify: `build_report_2025.py:298-403`
- Modify: `tests/test_backtest_timeframes.py`

**Interfaces:**
- Produces: `build_report_2025(strategy_results, per_strategy_details, overall_best, *, report_label="2025 Backtest", data_source="Alpaca SIP historical data") -> str`

- [ ] **Step 1: Write a failing report-label/source test**

```python
def test_report_accepts_history_label_and_source():
    html = build_report_2025({}, {}, None,
        report_label="2016–Present Backtest",
        data_source="Alpaca SIP historical data")
    assert "2016–Present Backtest" in html
    assert "Alpaca SIP historical data" in html
    assert "yfinance live data" not in html
```

- [ ] **Step 2: Run and verify failure from unexpected keyword arguments**

Run: `.venv\Scripts\python.exe -m pytest tests/test_backtest_timeframes.py::test_report_accepts_history_label_and_source -q`

Expected: FAIL with unexpected keyword argument.

- [ ] **Step 3: Add keyword parameters and interpolate them into title, badge, and footer**

Keep the default `report_label="2025 Backtest"` so existing annual callers and
their string replacement remain compatible.

- [ ] **Step 4: Run report tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_backtest_timeframes.py -q`

Expected: all pass.

### Task 5: Build the 2016–present runner and yearly JSON summary

**Files:**
- Create: `backtest_history.py`
- Create: `tests/test_backtest_history.py`

**Interfaces:**
- Produces: `validate_range(start: date, end: date) -> None`
- Produces: `yearly_stats(trades: list[Trade], start_year: int, end_year: int) -> dict[int, dict]`
- Produces: `run_history_backtest(start: date, end: date, strategy_name: str | None = None) -> dict`
- Produces: CLI `python backtest_history.py [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--strategy NAME]`

- [ ] **Step 1: Write failing tests for date validation and realized yearly grouping**

```python
def test_rejects_history_before_alpaca_boundary():
    with pytest.raises(ValueError, match="2016-01-01"):
        validate_range(date(2015, 12, 31), date(2016, 2, 1))


def test_yearly_stats_group_accepted_trades_by_exit_year():
    trades = [trade(entry="2020-12-31", exit="2021-01-04", pnl=10),
              trade(entry="2021-06-01", exit="2021-06-02", pnl=-4)]
    result = yearly_stats(trades, 2020, 2021)
    assert result[2020]["trades"] == 0
    assert result[2021]["total_pnl"] == 6
```

- [ ] **Step 2: Run and verify import failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_backtest_history.py -q`

Expected: FAIL because `backtest_history` does not exist.

- [ ] **Step 3: Implement validation, yearly aggregation, and JSON-safe metrics**

Use `compute_stats` and `compute_max_drawdown`. Remove `_trades`; convert
non-finite profit factor to `None` before `json.dumps(payload, allow_nan=False)`.

- [ ] **Step 4: Run focused tests and verify pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_backtest_history.py -q`

Expected: PASS.

- [ ] **Step 5: Add a failing orchestration test with partial ticker history**

Monkeypatch `download_history`, `backtest_ticker`, `build_report_2025`, and the
output paths. Return no ARM bars before 2023 and assert ARM is skipped only for
the missing interval, other ticker trades remain, portfolio capping is called
once per strategy across the full range, and JSON contains cumulative/yearly
sections plus actual bar dates.

- [ ] **Step 6: Run the orchestration test and verify missing runner behavior**

Run: `.venv\Scripts\python.exe -m pytest tests/test_backtest_history.py -q`

Expected: FAIL at the first unimplemented orchestration assertion.

- [ ] **Step 7: Implement data loading, strategy execution, output, and CLI**

Load each `(ticker, timeframe)` once. Reuse `backtest_ticker`,
`apply_portfolio_cap`, `compute_stats`, and `build_report_2025`. Write
`reports/backtest_2016_present.html` and `.json`; print cumulative and per-year
tables. Return the JSON payload for tests and programmatic callers.

- [ ] **Step 8: Run runner tests and the full unit suite**

Run: `.venv\Scripts\python.exe -m pytest -q`

Expected: all tests pass with no warnings introduced by this feature.

### Task 6: Document, release, download, and run

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `CHANGELOG.md`
- Modify: `VERSION` via `scripts/version.ps1`
- Local artifacts: `cache/market_data.db`, `reports/backtest_2016_present.html`, `reports/backtest_2016_present.json`

**Interfaces:**
- Documents: `python backtest_history.py`

- [ ] **Step 1: Document the persistent cache, SIP/IEX split, command, ARM IPO behavior, and output paths**

Add the history runner to quick start and file structure. State that the first
run downloads 2016–present SIP bars and subsequent runs fetch missing tails
only; live bot behavior is unchanged.

- [ ] **Step 2: Bump patch version and fill the generated changelog section**

Run: `pwsh scripts\version.ps1 -Bump patch`

Expected: `VERSION` changes from `0.9.0` to `0.9.1` and a dated changelog section
is scaffolded. Move the Unreleased implementation bullets into Added/Changed.

- [ ] **Step 3: Run the actual history backtest to populate the cache and artifacts**

Run: `.venv\Scripts\python.exe backtest_history.py`

Expected: SIP series for both `4h` and `1d` are cached, ARM begins in 2023, all
seven strategies finish, and both report files exist.

- [ ] **Step 4: Prove the second run is cache-only**

Capture cache status and Alpaca download logs, rerun the same command, and
confirm no historical fetch is issued for already-covered ranges.

- [ ] **Step 5: Run final verification**

Run: `.venv\Scripts\python.exe -m pytest -q`

Run: `.venv\Scripts\python.exe -m py_compile data_feed.py market_cache.py backtest_2025.py backtest_history.py build_report_2025.py`

Run: `pwsh scripts\manage.ps1 status`

Expected: tests and compilation pass; bot/dashboard status is reported without
starting or duplicating any process.

- [ ] **Step 6: Inspect artifacts and repository diff**

Validate JSON with `ConvertFrom-Json`, confirm the report title and data source,
run `git diff --check`, and ensure `cache/` and `reports/` are not staged.

- [ ] **Step 7: Commit, verify tag, and push**

```powershell
git add .gitignore data_feed.py market_cache.py backtest_2025.py backtest_history.py build_report_2025.py tests README.md AGENTS.md CHANGELOG.md VERSION docs/superpowers/plans/2026-07-18-historical-cache-backtest.md
git commit -m "feat: cache and backtest full Alpaca history"
git status --short
git log -1 --oneline
git tag --points-at HEAD
git push --follow-tags
```

Expected: clean tracked worktree, automatic `v0.9.1+build<N>-<datetime>` tag, and origin/main
contains both commit and tag.
