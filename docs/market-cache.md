# Adjusted historical cache — F09

Starting with v0.24.14, each `(symbol, timeframe, feed, adjustment)` cache series
contains one complete fetched generation. Prices fetched at different times are
no longer joined by downloading only missing edges.

## Refresh and failure behavior

The entire union of the existing and requested ranges is downloaded and replaced
when a request extends either boundary, on the first request of a new UTC day,
when `refresh=True` is requested, or when legacy coverage lacks snapshot metadata.
A covered request on the same UTC day reuses its generation. The data ceiling
still excludes the current UTC day.

Replacement removes old rows absent from the new response, including provider
withdrawals. Only rows within the requested full range are stored. Missing
columns/timestamps, nonfinite values, invalid prices/volume, and an empty response
that would erase populated history fail the refresh. Successful empty histories
(for example, before an IPO) remain cacheable.

A SQLite transaction covers the coverage check, provider call, replacement,
snapshot metadata, read manifest and returned read. Failed downloads or writes
roll back the entire refresh. The request raises; it does not present stale data
as a successful refresh. Concurrent cache writers cannot merge generations or
publish a successful prefix after a failed suffix. WAL readers may see the old
committed generation while refresh is in progress.

This conservative implementation serializes cache calls across symbols,
including hits. Competing writers wait up to 120 seconds before raising a lock
error. Refreshing a large minute-history range can cost substantial download
time and block competing cache calls. Full-range replacement is intentional:
these data adapters supply no adjustment-version token the cache can use to
prove that a newly downloaded edge matches older rows.

## Identifying data used by research

Returned frames retain `timeframe`, `feed`, and `adjustment` attributes and add:

- `cache_snapshot`: snapshot ID, full fetched coverage, timestamp, row count,
  SHA-256 content fingerprint, and fingerprint-format version.
- `cache_read_run_id`: process-scoped read group, shared across cache instances.

Every successful nonempty-range request is logged and persisted in `cache_reads`
with its process group, PID, consumer filename, exact requested bounds and snapshot
ID. `cache_snapshots` retains the corresponding metadata even after newer prices
replace that generation. `status()` exposes the current generation's ID/hash.

Export the reads for a logged process group using:

```python
import json
from pathlib import Path
from market_cache import MarketDataCache

cache = MarketDataCache()
manifest = cache.read_manifest(run_id="the-group-id-from-the-run-log")
Path("research-data-manifest.json").write_text(
    json.dumps(manifest, indent=2), encoding="utf-8"
)
```

Calling `read_manifest()` without an ID returns this process's reads. A process
can contain several years, strategies or experiments; the group is not a unique
portfolio/variant ID. Use read ranges/times and the experiment's own metadata to
associate those reads with a particular result.

Fingerprints use pandas' row hashing over normalized float64 OHLCV values and
UTC nanosecond timestamps, followed by SHA-256. The pandas version and format
are recorded. These are audit fingerprints, not provider corporate-action IDs.
Historical metadata is retained, **not old price rows**; archive the actual
frames separately when exact offline replay is required.

## Limits and migration

F09 repairs cache-created discontinuities within a series. It cannot guarantee
that a provider returns a globally atomic snapshot across pages, symbols,
timeframes or separate requests. A same-day revision of an already covered
range is picked up by explicit refresh or the next day's refresh. The manifest
makes different reads identifiable; it does not pin a whole research process to
one provider revision. Cross-feed alignment remains F10.

Existing cache schemas migrate additively. Unversioned coverage is refreshed
before being served, so an old mixed series is not treated as trustworthy merely
because all requested dates are present. This release did not refresh the real
market-data history or regenerate old reports. The correction takes effect on
subsequent cache requests; saved historical results retain their original inputs.
