# Equity-Percentage Position Sizing Design

## Objective

Replace the live bot's fixed `$200` entry budget with a maximum of 20% of the
current Alpaca account equity per new position. Make annual backtests use the
same 20% sizing rule with whole shares and compound realized profit and loss
within each calendar year, while resetting every year to a fresh `$1,000`.

## Decisions

- Existing entry order types and protective exits remain unchanged. The sizing
  change does not convert market entries into limit-price entries.
- Live sizing uses one fresh Alpaca account snapshot per bot cycle. Each order
  budget is the lesser of 20% of snapshot equity and locally remaining cash.
  Submitted order budgets reduce that local cash immediately so multiple
  signals in one cycle cannot depend on broker settlement timing or use margin.
- A current valid market snapshot supplies the sizing price for every live
  strategy. Quantity is rounded down to a whole share. An allocation that
  cannot buy one share is skipped without submitting an order or notification.
- Annual backtests start with `$1,000`, compound realized profit and loss within
  that calendar year, and reset to `$1,000` for the next calendar year. No
  strategy carries accumulated capital from one reported year into another.
- Backtests enforce whole-share sizing. Positions with one or two shares use
  the live single-bracket exit lifecycle; positions with at least three shares
  use the three-target scale-out lifecycle. All exit legs belong to one
  portfolio position and consume one concurrency slot.
- Backtest sizing equity is `$1,000` plus P&L realized so far in that annual
  simulation. This avoids look-ahead and provides deterministic intrayear
  compounding without introducing a full cross-ticker mark-to-market engine.
- Available backtest cash is tracked separately from equity. It decreases when
  a position opens and increases as shares exit, preventing overlapping trades
  from using more than the unlevered portfolio can fund.
- Existing fixed-dollar historical output is superseded. Reports and dashboard
  metadata describe the 20% rule and show each annual run's starting equity,
  ending equity, return percentage, and dollar P&L.

## Architecture

### Shared sizing policy

`config.py` exposes exact portfolio settings rather than a fixed trade amount:

- `initial_backtest_equity = 1000.0`
- `position_size_pct = 0.20`
- `max_concurrent_positions = 5`

A focused sizing helper validates equity, available cash, percentage, and
reference price, then returns an integer quantity and reserved budget. It never
returns a quantity whose reference-price notional exceeds the lesser of the
percentage allocation and available cash.

### Live sizing snapshot

At the beginning of a live bot cycle, the bot obtains account equity and cash.
If that read fails or returns non-finite/non-positive equity, new entries are
disabled for that cycle, but signal evaluation and existing-position exit
reconciliation continue. The bot fetches a live snapshot immediately before
each eligible entry and uses its price solely for quantity sizing.

Before a parent order is submitted, the bot persists its client-id ownership
intent. After broker acceptance, it attaches the broker id and deducts the
reference-price notional from locally remaining cash. Rejected parents close
their durable intent without reserving cash. If submission returns an
ambiguous timeout, the bot looks up the same client id and adopts the broker
order when found. An unresolved lookup leaves the intent pending, disables
later entries for the cycle, and lets reconciliation retry after a restart. A
pending intent is retired only after a grace period and explicit confirmation
from Alpaca that the client id is absent. Logging records equity, percentage
budget, cash cap, price, and final whole-share quantity without exposing
account credentials.

Before loading the next account sizing snapshot, the singleton checks durable
entries across every strategy. Any active or unverifiable parent order disables
new entries for that cycle. This prevents an accepted-but-unfilled order from a
prior loop, restart, or strategy swap from reusing its cash or position slot.

Scaled entries use an atomic stop-only OTO market parent. Its cash/position
slot is reserved before the three separate profit targets are submitted. If
target setup is incomplete, partial targets must reach a confirmed terminal
state; otherwise later entries are disabled and reconciliation runs
immediately while the durable trade and broker-held parent stop remain active.

### Annual portfolio backtest engine

A dedicated portfolio module accepts chronological entry candidates for one
strategy and one calendar year. Candidates retain their entry signal and the
price data needed to simulate the correct exit lifecycle after their integer
quantity is known.

Before each entry timestamp, the engine realizes every accepted exit leg from
a strictly earlier timestamp. All simultaneous candidates are then sized from
the same pre-event account so a same-bar exit cannot leak future P&L into
another ticker's opening quantity. It computes sizing equity from the
initial `$1,000` plus realized P&L, caps the 20% allocation by available cash,
rounds down to whole shares, and rejects entries when:

- fewer than one share can be purchased;
- five positions are already open;
- the same ticker already has an open position; or
- equity, cash, entry price, or the derived budget is invalid.

For one or two shares, the engine simulates the existing initial stop and final
take-profit bracket. For three or more shares, it uses the existing three-way
quantity split, scaled targets, and stepped-stop exit simulation. Signal-exit
strategies use their existing next-session entry, emergency stop, and opposite
signal exit lifecycle at every quantity.

Each accepted exit produces the existing `Trade` records with actual integer
shares and dollar P&L. Portfolio metadata includes starting equity, ending
equity, return, accepted positions, skipped positions, and an equity curve for
drawdown calculation.

### Annual and historical runners

The 2024, 2025, and 2026 runners each invoke the annual portfolio engine once
per strategy with `$1,000`. The historical runner partitions candidates by
calendar year and invokes an independent `$1,000` simulation for each year.
Its cumulative comparison sums independently generated annual P&L; it does not
represent cross-year compounding. The JSON output records the reset policy so
downstream consumers cannot mistake the aggregate for one continuously
compounded account.

The research optimizer uses the same annual portfolio engine and no longer
tunes a fixed dollar amount. Strategy signal parameters remain independently
tunable.

### Dashboard and documentation

The dashboard API exposes `position_size_pct`, `initial_backtest_equity`, and
`max_concurrent_positions`. Home metadata reads `20% equity/trade` rather than
`$200/trade · $1,000 cap`. Backtest report summaries include starting equity,
ending equity, return percentage, and P&L. README, `program.md`, and agent
guidance describe annual-reset compounding and the live whole-share constraint.

## Error Handling and Safety

- Live account or price failures suppress new entries only; they never bypass
  protected exits or close positions.
- The live cash cap and backtest cash ledger prohibit margin usage.
- All numeric sizing inputs must be finite and positive. Percentage must be in
  `(0, 1]`, and concurrent-position capacity must be a positive integer.
- A market fill may differ from the sizing snapshot. Whole-share flooring keeps
  expected notional at or below the 20% budget; normal market slippage remains
  possible and is logged through the existing fill reconciliation.
- Existing singleton process management remains authoritative. Bot and
  dashboard restarts occur only through `scripts/manage.ps1`.

## Testing and Verification

Test-first coverage verifies:

- 20% quantity calculation, available-cash capping, whole-share flooring,
  invalid inputs, and high-priced skips;
- a profitable closed trade increases a later allocation in the same year;
- a losing trade decreases a later allocation in the same year;
- overlapping positions cannot exceed available cash or five slots;
- three exit legs consume one position slot;
- one- and two-share positions use single-bracket outcomes while three-or-more
  positions use scale-out outcomes;
- every historical year restarts at `$1,000` and does not inherit prior-year
  profit or loss;
- live entry evaluation continues safely when account sizing data is missing,
  with existing-position reconciliation still called;
- dashboard and report metadata display the percentage-sizing policy.

Verification runs the focused tests, the full pytest suite, all annual
backtests, and the historical backtest from the local SIP cache. Generated
results are inspected for integer quantities, annual start/end equity, and
reset metadata. After code and dashboard changes, the managed dashboard and
the currently configured ensemble bot are restarted and must report HEALTHY.
