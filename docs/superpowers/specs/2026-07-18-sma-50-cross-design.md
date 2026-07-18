# SMA 50 Cross — Design

Date: 2026-07-18

## Goal

Add a seventh strategy named `sma_50_cross` with the dashboard label **SMA 50 Cross**. It trades long positions from completed daily candles:

- Buy after the daily close crosses from at or below SMA(50) to above SMA(50).
- Sell after the daily close crosses from at or above SMA(50) to below SMA(50).
- Attach a broker-held 10% emergency stop to every entry.
- Do not take profit at a fixed target, use a time stop, or open short positions.

The existing six strategies continue to use their current 4-hour candles and exit rules.

## Option evaluation

The options were tested on the configured universe (NVDA, AMZN, META, AMD, and ARM) from 2024-01-01 through 2026-07-17. The comparison used adjusted daily candles, a 50-day SMA, signals at the completed close, next-open execution, $200 per trade, and 5 basis points of cost on each side.

| Variant | Trades | P&L | Win rate | Realized max drawdown |
|---|---:|---:|---:|---:|
| Long-only cross + 10% emergency stop | 108 | $1,139.05 | 33.3% | $176.87 |
| Pure long-only cross | 108 | $1,128.73 | 33.3% | $194.32 |
| Long/short reversal | 214 | $764.54 | 27.6% | $364.32 |
| Cross entry + existing TP/time-stop exits | 369 | $363.96 | 58.8% | $213.92 |

The selected long-only stop-protected variant had the highest P&L and the lowest drawdown. Shorting was rejected because it underperformed, materially increased drawdown, requires margin and stock availability, and introduces theoretically unlimited loss. The existing take-profit/time-stop overlay was rejected because it cut trends early and obscured the requested rule.

These results select among the requested implementation variants; they are not a claim of future profitability.

## Strategy semantics

`SMA(50)` is the arithmetic mean of the latest 50 completed daily closing prices. Equality is neutral and does not trigger a signal.

An entry signal exists only when:

1. the previous completed close was less than or equal to its SMA(50); and
2. the latest completed close is greater than its SMA(50).

An exit signal exists only when:

1. the previous completed close was greater than or equal to its SMA(50); and
2. the latest completed close is less than its SMA(50).

The exact-cross requirement prevents repeated orders on every day that price remains on one side of the average. After an emergency stop, the strategy waits for a new down-cross followed by a new up-cross before re-entering.

The entry signal price is the completed daily close. Live execution is the next eligible market order, so the fill may differ. The protective stop submitted with the entry is 10% below the signal close. A cross-down exit cancels the remaining stop leg before selling only the quantity proven to belong to this bot.

## Architecture

### Strategy interface

`BaseStrategy` gains declarative metadata for `timeframe` and `exit_mode`, plus a default `check_exit` method that returns no signal. Existing strategies retain `timeframe="4h"` and `exit_mode="bracket"` without behavior changes.

`SMA50CrossStrategy` declares `timeframe="1d"` and `exit_mode="signal_with_stop"`. Its entry and exit checks contain only the two-bar SMA cross conditions. It uses the shared SMA and ATR columns, although ATR is informational and does not decide entry or exit.

### Market data

`data_feed.py` gains generic 4-hour/daily fetch functions while preserving the current 4-hour wrappers. Daily live evaluation removes the current session's candle, ensuring signals only use completed daily bars. The bot fetches the timeframe declared by the selected strategy.

Backtests cache history by `(ticker, timeframe)`. This lets one full run evaluate the existing strategies on 4-hour bars and SMA 50 Cross on daily bars without globally changing `BAR_TIMEFRAME`.

### Backtest lifecycle

Existing bracket strategies continue through the current scale-out engine. Signal-exit strategies use a separate, small lifecycle:

1. detect the cross at a completed close;
2. enter at the next available session open;
3. on later bars, apply the 10% stop first, including gap-through behavior;
4. detect a close cross below SMA(50) and exit at the next available session open;
5. close any still-open trade at the final data close as `end_of_data`.

This path emits one `Trade` per position and bypasses TP reachability, scale-out, and time-stop logic.

### Live orders and reconciliation

The entry uses an Alpaca OTO market order with only a stop-loss leg. OTO is the broker-supported way to activate a stop after an entry fills without adding a take-profit. The existing whole-share sizing and `$200/trade` skip behavior remain unchanged.

The live loop retains the indicator-enriched daily frames it fetched. During reconciliation, an open SMA 50 Cross trade is closed with reason `sma_cross_down` when its latest completed frame reports an exit cross. The normal ownership proof, stop cancellation, order correlation IDs, database recording, and notifications remain mandatory. Existing strategies continue through stepped-stop and time-stop reconciliation.

If the OTO stop fills first, the existing nested-leg reconciliation records the broker fill. If ownership cannot be proven or daily data cannot be fetched, the bot fails closed and leaves the position untouched.

### Dashboard and documentation

The registry is the source of the seventh strategy card, enabled/running pills, and strategy metadata. The card displays `Daily`, `SMA 50`, `10% emergency stop`, and `Cross-down exit`. Example charts fetch daily bars for this strategy and omit take-profit annotations. Reports and database backtest rows record `1d` for this strategy.

README, AGENTS, research log, CLI choices, colors, and strategy counts are updated from six to seven where applicable.

## Error handling and safety

- Fewer than 51 completed daily bars produces no signal.
- Missing or non-finite SMA/price data produces no signal.
- The live bot never acts on an incomplete daily candle.
- A stop price invalidated by an extreme opening gap causes the entry to be skipped rather than submitted without protection.
- Failed ownership verification blocks a crossover sale.
- Existing singleton process controls remain unchanged; dashboard and bot restarts use `scripts/manage.ps1` only.
- Paper trading remains hardcoded.

## Testing and acceptance

Automated tests cover:

- entry on an exact cross above;
- no entry while price merely remains above;
- exit on an exact cross below;
- no exit while price merely remains below;
- no signal before SMA(50) is fully populated;
- daily timeframe and registry metadata;
- current-session daily candle removal;
- backtest next-open entry, cross-down exit, stop exit, and no TP/time-stop exit;
- live OTO request shape and bypass of TP reachability;
- live crossover close calls ownership-protected `_close_owned`;
- existing strategy regression tests.

Acceptance requires all tests to pass, all three annual backtests to complete with the new strategy stored as timeframe `1d`, the dashboard and bot to be restarted through the manager after their files change, both services to report healthy, and the versioned commit and tag to reach the upstream branch.

## Sources

- Alpaca order documentation: https://docs.alpaca.markets/docs/orders-at-alpaca
- SEC short-sale risk bulletin: https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-bulletins-51
