# Earnings entry policy — v0.24.11

Trend Pullback suppresses entries during the configured `earnings_avoid_days`
preceding XNYS trading sessions and until the scheduled announcement has passed.
The default is three sessions. Holidays and weekends do not count as sessions;
every intraday candle is assessed against its actual decision time, not its row
position. The same policy applies to Trend Pullback's vote inside Ensemble.
Other Ensemble votes remain eligible. This is an entry filter; it does not
prevent exits or force existing positions to close.

## Announcement times

Event times retain their timezone and are interpreted in New York time:

- A known before-market release clears at that session's regular open.
- A known intraday or after-market release remains blocked until its scheduled
  timestamp. This is schedule-based avoidance, not confirmation of publication.
- A date-only or midnight placeholder blocks the whole event date.
- A weekend release uses the next trading session as its session anchor; prior
  sessions are blocked, and the release has passed by the next regular open.

For a Monday release, a three-session lookback normally starts Wednesday.
If Monday is a holiday and the release is Tuesday, the lookback starts Wednesday
of the previous week. Live evaluation uses the current decision time even when
the signal candle is old. Minute backtests evaluate at the modeled entry fill;
legacy comparisons use the next candle's timestamp. Dashboard examples use
signal-information availability and retain their existing illustrative exit model.

## Refresh and missing data

The live bot requests yfinance's current earnings schedule. Successful data is
valid for six hours; failed, empty or past-only results are unknown and retried
after five minutes, on the next bot cycle. Each actual refresh clears yfinance's
shared HTTP cache so a stale cached response cannot be stamped as newly observed.
This also evicts other yfinance HTTP entries in the same process.

Missing, stale, failed, or malformed calendar data suppresses the Trend Pullback
signal. A missing `near_earnings` mask also suppresses that strategy. Unknown
coverage is logged explicitly, and the dataframe records `earnings_status` as
`unknown`, `blocked`, `clear`, or `disabled`. Setting `earnings_avoid_days=0`
explicitly disables the policy for comparisons; it is not the production default.
Live calendar/storage failures leave exit reconciliation running.

## Historical observations

`cache/earnings.db` is an append-only SQLite archive of ticker, observation time,
validity end, event list, status and source. A historical decision selects the
latest observation at or before that decision, and uses it only while valid.
Later schedule revisions cannot change an earlier decision. Historical runs and
dashboard examples never fetch today's calendar to reconstruct an old schedule.

The repository has no pre-existing point-in-time earnings archive. Consequently,
older Trend Pullback opportunities are suppressed as unknown until independently
archived observations are supplied. Ensemble can still trade on its other votes.
Existing reported P&L and the old claimed earnings-filter benefit have not been
recomputed and are not evidence of improvement under the corrected policy.

An external, genuinely archived schedule can be imported through the store API:

```python
from earnings_calendar import CalendarStore

CalendarStore().record(
    "AMD",
    observed_at="2026-09-10T14:00:00Z",
    events=["2026-10-27T16:30:00-04:00"],
    valid_until="2026-09-10T20:00:00Z",
    source="Your archived provider snapshot identifier",
)
```

This is an API-format example, not a verified AMD announcement. Use the actual
recorded observation time and source, never a date inferred from today's
calendar. Imported successful empty lists explicitly certify a known empty
schedule during their validity window; an automatic empty response cannot do so.
The importer is responsible for the completeness and provenance of its archive.

## References

- [yfinance earnings-date API](https://ranaroussi.github.io/yfinance/reference/api/yfinance.Ticker.get_earnings_dates.html)
  exposes a current table, with no historical observation-time parameter.
- [yfinance source](https://github.com/ranaroussi/yfinance/blob/main/yfinance/base.py)
  and the installed library were checked for parsing and cache behavior.
- [Exchange Calendars](https://github.com/gerrymanoim/exchange_calendars) supplies
  the XNYS sessions used by the policy.
