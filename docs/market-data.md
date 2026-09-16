# Shared market-data policy — F10

From v0.24.15, `MARKET_DATA_FEED` in `config.py` selects the Alpaca feed for
live/recent bars, reference snapshots, annual and cumulative backtests, minute
execution/valuation data, and Alpaca-backed research downloads. Default: **IEX**.
An optional `MARKET_DATA_FEED=iex|sip` environment setting (including `.env`)
overrides it. Restart both services through `scripts/manage.ps1` after changing
the setting. Verify real-time subscription access before choosing SIP.

Requests always specify the selected feed. Invalid settings raise; a provider
failure never triggers a switch to another feed. Live downloads retain their
existing empty-result-on-error behavior; strict historical downloads raise.
IEX and SIP remain separate cache partitions, including minute execution data.
Explicit feed overrides remain available for comparative research.

## Session and provenance policy

Both paths use provider-native buckets, including trades outside regular hours;
there is no regular-hours-only slicing. Zero-volume buckets are removed. Signal
evaluation uses completed candles: daily bars strictly before the current New
York date, or 4h buckets whose start plus four hours has passed. Cached research
also excludes the current UTC day. A raw `fetch_bars` call may include a forming
bar; signal callers apply `completed_bars` before evaluation. This release does
not change bucket boundaries, strategy rules, or completion guards.

Bars request `adjustment=all`; live snapshots are current, unadjusted reference
prices. Frames retain feed/adjustment metadata; the annual loader adds session
policy, reports display it, and cumulative JSON includes structured policy.
Report rendering uses input-frame feed metadata when available rather than
relabeling an old dataset from a changed configuration. Bot runtime metadata
records the configured policy. The [cache manifest](market-cache.md) records
actual reads and fingerprints.

New annual database runs store a `data_source` label at run start, including
single-strategy runs without an HTML report. New saved equity curves receive
an explicit source label, including the mixed yfinance warmup where applicable.
The additive database migration leaves old records NULL (unknown); API results
expose the field. Existing dashboard cards do not yet display this label.

The daily equity-curve research helper still uses yfinance before 2016. This is
explicitly mixed-source history, not a single-feed replay of live operation.
That legacy stitched path is not validated end to end by this fix: concatenation
loses source attributes, so automatic minute-execution loading remains unsupported.
F10 does not establish equal revisions across separately fetched data, validate
bid/ask execution costs, or resolve the other open review findings.

## Account verification and bounded comparison

On 2026-09-15 at 18:44:13 UTC, read-only requests for NVDA minute bars covering
the preceding five minutes and for the latest snapshot both rejected SIP with
“subscription does not permit querying recent SIP data.” IEX returned four
minute bars, a latest trade stamped 18:44:03.835552 UTC (about nine seconds old
at probe start), and a quote stamped 18:44:13.248438 UTC with bid 211.98/ask
211.99. This is one access/freshness observation, not a latency guarantee or a
measurement of actual fill costs. No orders were submitted by the probe.

This supports preserving the existing IEX live feed and changing the default
historical source to IEX. Alpaca documents that historical SIP queries ending
at least 15 minutes earlier can be available without real-time SIP access:
[Alpaca market-data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq).

The [comparison artifact](feed-comparison-2026-09-15.json) measures August 2026
on NVDA, AMZN, META, AMD and ARM. Each source was fetched independently with
April 1–July 31 warmup, `adjustment=all`, and the same indicator implementation.
Raw Breakout entry predicates (4h) and SMA 50 Cross entry predicates (daily)
were compared at common UTC bar starts; nonmatching buckets are counted
separately. Source CSV SHA-256 fingerprints identify the fetched inputs.
These are entry-predicate comparisons before execution, TP reachability and
portfolio constraints; they are not strategy profit estimates or tuning trials.

Across 227 aligned 4h bars, Breakout disagreed on 14 entry decisions. Across
105 aligned daily bars, SMA 50 Cross had no entry disagreement in this sample.
Native histories remain independent when computing indicators, so different
bucket coverage contributes to signal differences, as it does in operation.
This sample does not establish equivalence for other strategies or dates.

Saved reports, existing backtest DB results and SIP caches were not rewritten.
Their old performance claims remain historical SIP results and must be rerun
on IEX before being used to assess current live inputs. New historical requests
populate the IEX partition; there is no automatic migration or reranking.
