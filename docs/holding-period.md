# Holding-period policy — F11

From v0.24.16, all `*_max_holding_days` configuration values count **XNYS
session closes strictly after the actual entry fill**. Existing field names and
numeric settings remain compatible; their unit is now explicitly trading sessions.
The shared implementation lives in `holding_period.py`.

The first close after entry counts as session one, including a partial entry
session. A fill exactly at a close starts counting with the next session. Exchange
holidays and weekends do not count. DST and early closes follow the existing
`exchange_calendars` XNYS schedule. This is a count of closes, not accumulated
6.5-hour days or a number of strategy candles.

Example: a fill on Wednesday November 26, 2025 at 20:00 UTC with a two-session
threshold becomes eligible at Friday November 28 at 18:00 UTC: Wednesday's close
is first, Thanksgiving is closed, and Friday closes early. The next regular-session
execution opportunity is Monday. A Friday fill with a two-session threshold becomes
eligible at Monday's close, assuming neither date is an exchange holiday.

Eligibility does not force a sale. The current reference/executable price must
be at least the actual entry basis. If the position is underwater, its protective
orders remain in force and the bot checks again later. Stop/target precedence and
owned-quantity safeguards remain unchanged. SMA 50 Cross and TQQQ Momentum keep
their signal-driven exits and do not acquire time stops.

## Live and simulated clocks

Live reconciliation records the verified broker entry order's `filled_at` in
the new nullable `trades.entry_filled_at` column. Acceptance/creation time and
the signal timestamp cannot authorize a time exit. Existing positions backfill
from their exact owned entry order during reconciliation. Missing, invalid or
future fill times suppress only the time-stop decision; protection management
and other exit reasons continue. A later update without a timestamp does not
erase a previously recorded fill time.

Normal backtests use the modeled minute-open entry fill. After the deadline,
the next available regular-session minute open at or above that basis permits
the time exit. A stale profitable signal close cannot queue an underwater exit
at a later opening gap. No new signal candle is required to reach the threshold.
Live polling (currently 30 minutes) and one-minute simulation still have different
observation cadence; equal eligibility does not promise identical fills, spreads
or market latency. A market order can still fill below its reference price.

The explicit legacy coarse-candle research simulator shares the same deadline,
using the modeled entry open or observed entry close. It evaluates at observed
candle closes and timestamps a time exit when that close is known. It remains a
coarse research approximation, not a session-execution replacement. `bars_held`
diagnostics remain candle counts in backtests; the legacy live elapsed-day
reporting helper is separate from time-stop decisions.

## Migration and evidence

The database change is additive and leaves unknown old fill times NULL. It does
not rewrite trades or cached backtests. Existing report profitability claims need
rerunning under the corrected session rule; this fix does not retune strategy
parameters or regenerate historical rankings.

Regression tests cover weekend/DST transitions, Thanksgiving, early closes,
year boundaries, exact-close fills, delayed fills and stale signal timestamps,
unchanged strategy limits, both bracket simulators, breakeven recovery, missing
fill timestamps, idempotent migration, and the real owned-protection lifecycle
with a simulated broker.
