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

## Fill assumptions

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
  Holding bars count completed **signal** candles after entry, not minutes. The
  separate disagreement between bars and calendar holding days remains F11.
- End-of-window liquidation uses the last complete regular-session minute close.
  No OHLC from a minute ending beyond the requested boundary is used. Portfolio
  cash reuse remains conservative for events with identical timestamps.

These are bar-based assumptions, not a simulation of the order book. Spread,
fees, queue position, partial fills, latency and market impact remain unmodeled.
The entry drift guard is an acceptance filter, not a transaction-cost estimate.
The daily-loss valuation issue remains F07. Different adjustment vintages in the
incremental cache remain F09; matching feed/adjustment keys does not fix that.

## Running and extending backtests

Install `requirements.txt` before running the existing backtest commands. The
first run for a symbol/year needs more data and storage to populate minute
history; later runs reuse the persistent cache. Fetch failures or entirely
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
