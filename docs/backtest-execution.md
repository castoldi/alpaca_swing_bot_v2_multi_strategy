# Backtest execution model

As of v0.24.9, annual, historical, and research candidate collection uses separate
strategy candles and one-minute execution bars. Strategy indicators still use
their existing timeframe. Execution bars use the signal data's Alpaca feed and
`adjustment=all`, cached under the separate `1min` series in `cache/market_data.db`.
The optimizer now uses the shared SIP history loader so its signal and execution
prices share provenance; its existing daily-timeframe selection is still F15.

## Signal and execution clocks

All event timestamps are timezone-naive UTC. A 4h candle becomes available four
hours after its start. An Alpaca daily candle is considered available at the next
New York midnight, since its aggregation can include extended-hours trades.
An order then fills at the first available regular-session minute open at or
after signal availability. No earlier minute can influence the entry or exits.
The entry drift guard uses this executable open, and the signal's absolute
bracket stop/target prices are preserved. Signal-exit strategies retain their
fill-relative emergency stops.

`exchange-calendars` supplies the XNYS core-session schedule, including DST,
weekends, holidays, extraordinary closures, and early closes. Only positive-volume
minute bars starting at/after the open and before the close are executable. The
16:00 ET minute (13:00 on an early-close day) is excluded. Missing trading bars
are not fabricated; the next available eligible minute is used.

## Live signal timing (v0.25.0)

The live bot applies the same clock (`bot._signal_fill_time`):

- **Every completed bar is evaluated once.** A per-strategy/ticker cursor
  (`signal_cursor` table) records the last evaluated bar. Each cycle examines
  every bar completed since then, oldest first, so bars that complete while the
  market is closed (the regular-session close bar, after-hours buckets) are not
  skipped. With no cursor (first run) only the latest bar is examined.
- **A signal is actionable only in its fill window.** That window starts at the
  modelled fill time (first regular-session minute at/after availability) and
  lasts one loop interval plus 5 minutes' grace. A skipped entry (slippage,
  sizing, tax) is not retried on the same bar, and an exit is never followed by
  a re-entry on the same bar. Signals from bars missed while the bot was down
  are dropped, which the backtest does not model.
- **Signal exits latch.** `signal_with_stop` strategies exit on the oldest exit
  signal from any completed bar after the entry's signal bar, matching the
  backtest's pending-exit rule.


- A sell stop already crossed at a minute's open fills at that open. It does not
  receive a better, unavailable stop price after a downward gap.
- A target already marketable at the open fills at the open, before any later
  same-minute low. This assumes immediate full fills and available liquidity.
- Otherwise, a minute touching both barriers uses the stop first, since OHLC
  does not reveal the path. Intraminute fills are recorded at minute end so the
  resulting cash cannot finance an earlier entry.
- Research scale-out uses the same execution minutes. Stop ratchets become
  effective on the following minute, and gaps through a raised stop fill at the
  opening price for the remaining quantity.
- Strategy exits and time-stop decisions use completed signal candles and fill
  at the next eligible minute open. An opening protective fill takes precedence.
  If an opposite strategy signal arrives while an entry waits overnight, the
  modeled entry fills and closes at that same eligible open; the pending exit
  is preserved even though it predates the entry fill. This assumes immediate
  full fills at the open, without order cancellation or broker-latency modeling.
  Holding bars count completed **signal** candles after entry, not minutes. The
  separate disagreement between bars and calendar holding days remains F11.
- End-of-window liquidation uses the last complete regular-session minute close.
  No OHLC from a minute ending beyond the requested boundary is used. Portfolio
  cash reuse remains conservative for events with identical timestamps.

These are bar-based assumptions, not a simulation of the order book. Spread,
fees, queue position, partial fills, latency and market impact remain unmodeled.
The entry drift guard is an acceptance filter, not a transaction-cost estimate.
F09 now replaces full cached ranges when extending or refreshing adjusted history
and records snapshot fingerprints. The [cache policy](market-cache.md) explains
refresh timing, manifests, migration and the limits of provider consistency.

## Daily-loss valuation (F07, v0.24.12)

The daily entry guard uses the same regular-session minute price source as
execution. A minute's open is observable at its start; its close is observable
one minute later. At a shared boundary the next minute's open takes precedence.
The 4h or daily signal candle's eventual close cannot affect an earlier entry.
If no new minute is available, valuation carries the last observed price;
this is a sparse-data approximation, not an assertion of a fresh broker quote.

Each New York trading date compares current equity with the prior XNYS session
close, including holidays, DST and early closes. Before valuing that baseline,
the portfolio replays all exits through and including the previous close. It
then processes today's exits. This ordering applies even across several sessions
without entry candidates: a loss before today's first candidate remains today's
loss, and a loss realized in a prior session is already in the baseline.

Exits at the exact entry timestamp count at their actual proceeds for risk
accounting, while their cash remains unavailable for sizing that simultaneous
entry under the existing conservative reuse rule. The guard is reevaluated at
each entry timestamp; recovery below the loss threshold permits entries again.
It never blocks exits. Reported trip days count dates with a breached entry
evaluation, not breaches during periods when no entry was considered.

`run_annual_portfolio(price_frames=...)` loads matching minute history for
signal frames carrying timeframe/feed/adjustment metadata. Custom callers can
instead pass one-minute OHLCV frames with `attrs['timeframe'] = '1min'`. Synthetic
observation series must be close-only and explicitly set
`attrs['price_timestamps'] = 'observed'`; their timestamps mean when prices were
known, not when candles started. Ambiguous custom bars and missing prices for
held inventory raise an error instead of silently using future closes or valuing
shares at zero. Runs without price frames still leave this guard disabled;
unifying the historical/optimizer risk configuration remains F15.

## Running and extending backtests

Install `requirements.txt` before running the existing backtest commands. The
first run for a symbol/year needs more data and storage to populate minute
history; covered requests reuse a fresh snapshot during the same UTC day.
Extensions and refreshes download the full covered union. Fetch failures or entirely
missing execution data stop the run rather than substituting strategy OHLC.
Pre-2016 or custom-price history needs matching execution data supplied explicitly.

For custom frames, pass `execution_bars=<one-minute OHLCV frame>` to
`collect_backtest_candidates` or `backtest_ticker`. Signal timestamps must follow
the strategy timeframe and UTC conventions above. Alpaca-loaded frames retain
feed/timeframe/adjustment metadata so execution history can be loaded automatically.
An explicit `legacy_execution=True` is available for synthetic fixtures or
comparing the old coarse-candle model; production entry points do not enable it.
The low-level `simulate_exit` helpers assume their caller supplies eligible bars;
the regular-session calendar is enforced by the default candidate collector.

Existing saved reports and database results have **not** been regenerated. They
remain historical outputs of their original model; rerun a backtest to apply the
corrected execution assumptions.

## References

- [Alpaca order rules](https://docs.alpaca.markets/us/docs/orders-at-alpaca): sell
  stops become market orders, targets are limit orders, and brackets do not support
  extended hours.
- [Alpaca market-data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq):
  minute and daily aggregation timestamps and trade-condition rules.
- [Exchange Calendars](https://github.com/gerrymanoim/exchange_calendars): XNYS
  schedule, session opens/closes and trading-minute conventions.
