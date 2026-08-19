"""Self-contained P&L ledger for this bot's own trades.

The Alpaca key is shared with several other projects, so broker equity can never
answer "how is THIS bot doing" — it moves on other people's trades. Every number
in this module is derived from the local `trades` table alone. The broker is
consulted only to *confirm* the bot's own trades (see `broker_sync.py`), never to
supply the totals.

Capital base
------------
The bot has no segregated account, so its book is a notional one: the capital
base is the most capital the bot ever had at risk simultaneously
(`peak_deployed_capital`) — i.e. the account size a standalone copy of this bot
would have needed to place exactly these trades. It is a running maximum over
the whole history, so it only ever grows; when it grows, previously reported
percentages restate downward. That is why the stored balance history keeps
**dollars** (which never restate) and derives percentages on read.

Everything here is pure: it takes trade dicts and returns values, with no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Iterable, Optional


# ── Trade-level helpers ───────────────────────────────────────────────────────

def effective_entry_price(trade: dict) -> float:
    """Real broker fill when recorded, else the signal-close entry price.

    Mirrors `bot._effective_entry_price` — cost basis must use what was actually
    paid, not the price the signal was computed from.
    """
    try:
        filled = trade.get("entry_filled_price")
        if filled:
            return float(filled)
        return float(trade.get("entry_price") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def shares_of(trade: dict) -> float:
    try:
        return float(trade.get("shares") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def cost_basis(trade: dict) -> float:
    """Dollars the bot committed to this trade at entry."""
    return effective_entry_price(trade) * shares_of(trade)


def is_real_trade(trade: dict) -> bool:
    """False for intent rows that never became a position.

    A rejected or unfilled entry is still written to `trades` and closed with
    zero shares (`entry_not_submitted`, `entry_not_filled`) so the intent is
    durable. Those rows are bookkeeping, not trades, and must not dilute the
    win rate or the trade count.
    """
    return shares_of(trade) > 0


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse a DB timestamp to an aware UTC datetime, or None.

    The table mixes naive local-ish strings written by backtest-shaped code
    ('2026-08-05 20:00:00') with aware ISO strings written by the live bot
    ('2026-08-10T14:11:18.427806+00:00'). Naive values are read as UTC so the
    two orderings are comparable.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip())
        except (TypeError, ValueError):
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_closed(trade: dict) -> bool:
    return str(trade.get("status") or "").lower() == "closed"


def is_open(trade: dict) -> bool:
    return str(trade.get("status") or "").lower() == "open"


def pnl_of(trade: dict) -> float:
    try:
        return float(trade.get("pnl_dollars") or 0.0)
    except (TypeError, ValueError):
        return 0.0


# ── Capital base ──────────────────────────────────────────────────────────────

def peak_deployed_capital(trades: Iterable[dict]) -> float:
    """Most capital held at risk at any one moment across the whole history.

    Walks entry (+basis) and exit (-basis) events in time order. At an identical
    timestamp entries are applied before exits, so a same-instant close-and-open
    is charged for both: freed cash is not reliably available to the next entry
    (settlement lags), and this number is meant to be the capital the bot would
    actually have needed.
    """
    events: list[tuple[datetime, int, float]] = []
    for trade in trades:
        if not is_real_trade(trade):
            continue
        basis = cost_basis(trade)
        if basis <= 0:
            continue
        entered = _parse_ts(trade.get("entry_date"))
        if entered is None:
            continue
        # sort key 0 = entry, 1 = exit → entries first on ties
        events.append((entered, 0, basis))
        exited = _parse_ts(trade.get("exit_date"))
        if is_closed(trade) and exited is not None:
            events.append((max(exited, entered), 1, -basis))

    events.sort(key=lambda e: (e[0], e[1]))
    current = 0.0
    peak = 0.0
    for _, _, delta in events:
        current = max(0.0, current + delta)
        peak = max(peak, current)
    return peak


# ── Snapshot ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OpenPosition:
    ticker: str
    trade_id: int
    strategy: str
    entry_date: str
    shares: float
    entry_price: float
    cost_basis: float
    mark: Optional[float]          # last price used, None when unavailable
    market_value: Optional[float]
    unrealized_pnl: Optional[float]
    unrealized_pct: Optional[float]
    broker_status: str             # confirmed | mismatch | missing | unverified


@dataclass(frozen=True)
class Snapshot:
    """Everything the bot knows about its own book, computed locally."""
    ts: str
    strategy: Optional[str]
    starting_capital: float
    realized_pnl: float
    unrealized_pnl: float
    equity: float
    total_pnl: float
    total_return_pct: float        # fraction, not percent
    open_count: int
    open_cost_basis: float
    open_market_value: float
    closed_count: int
    wins: int
    losses: int
    win_rate: float                # fraction
    gross_profit: float
    gross_loss: float
    profit_factor: float
    avg_pnl_pct: float             # fraction, mean across closed trades
    best_trade: float
    worst_trade: float
    total_deployed: float          # summed entry notional across closed trades
    return_on_deployed: float      # fraction; realized / total_deployed
    first_trade_at: Optional[str]
    last_trade_at: Optional[str]
    marks_complete: bool           # False when a mark was missing (equity is a floor)
    broker_confirmed: int
    broker_mismatched: int
    positions: tuple[OpenPosition, ...] = field(default=())

    def as_dict(self) -> dict:
        d = asdict(self)
        d["positions"] = [asdict(p) for p in self.positions]
        return d


def _profit_factor(gross_profit: float, gross_loss: float) -> float:
    """Gross profit per dollar of gross loss.

    With no losses there is no finite ratio; report gross profit itself rather
    than infinity, which neither SQLite nor JSON can round-trip. Matches the
    convention already used by `db.portfolio_stats`.
    """
    if gross_loss > 0:
        return round(gross_profit / gross_loss, 4)
    return round(gross_profit, 2) if gross_profit > 0 else 0.0


def build_snapshot(
    trades: Iterable[dict],
    marks: Optional[dict[str, float]] = None,
    *,
    strategy: Optional[str] = None,
    broker_status: Optional[dict[int, str]] = None,
    starting_capital: Optional[float] = None,
    now: Optional[datetime] = None,
) -> Snapshot:
    """Compute the bot's full P&L picture from local trades plus price marks.

    `marks` maps ticker → last price. A position whose ticker has no usable mark
    contributes its cost basis to market value and **nothing** to unrealized
    P&L, and flips `marks_complete` to False: an unknown mark must never be
    guessed into the headline number, so equity reads as a floor rather than a
    fabricated figure.

    `broker_status` maps trade id → the confirmation verdict from `broker_sync`;
    absent ids read as "unverified".
    """
    trades = list(trades)
    marks = marks or {}
    broker_status = broker_status or {}
    now = now or datetime.now(timezone.utc)

    real = [t for t in trades if is_real_trade(t)]
    closed = [t for t in real if is_closed(t)]
    opened = [t for t in real if is_open(t)]

    realized = sum(pnl_of(t) for t in closed)
    wins = sum(1 for t in closed if pnl_of(t) > 0)
    losses = sum(1 for t in closed if pnl_of(t) <= 0)
    gross_profit = sum(pnl_of(t) for t in closed if pnl_of(t) > 0)
    gross_loss = sum(-pnl_of(t) for t in closed if pnl_of(t) < 0)
    total_deployed = sum(cost_basis(t) for t in closed)

    pcts = []
    for t in closed:
        try:
            pcts.append(float(t.get("pnl_pct") or 0.0))
        except (TypeError, ValueError):
            pass

    positions: list[OpenPosition] = []
    unrealized = 0.0
    open_market_value = 0.0
    marks_complete = True
    for t in opened:
        basis = cost_basis(t)
        entry = effective_entry_price(t)
        qty = shares_of(t)
        raw_mark = marks.get(str(t.get("ticker") or ""))
        try:
            mark = float(raw_mark) if raw_mark is not None else None
            if mark is not None and (mark <= 0 or mark != mark):  # NaN-safe
                mark = None
        except (TypeError, ValueError):
            mark = None

        if mark is None:
            marks_complete = False
            mv = None
            upnl = None
            upct = None
            open_market_value += basis      # carry at cost, claim no gain
        else:
            mv = mark * qty
            upnl = mv - basis
            upct = (upnl / basis) if basis else 0.0
            unrealized += upnl
            open_market_value += mv

        positions.append(OpenPosition(
            ticker=str(t.get("ticker") or ""),
            trade_id=int(t.get("id") or 0),
            strategy=str(t.get("strategy") or ""),
            entry_date=str(t.get("entry_date") or ""),
            shares=qty,
            entry_price=entry,
            cost_basis=basis,
            mark=mark,
            market_value=mv,
            unrealized_pnl=upnl,
            unrealized_pct=upct,
            broker_status=broker_status.get(int(t.get("id") or 0), "unverified"),
        ))

    base = starting_capital if starting_capital else peak_deployed_capital(trades)
    total_pnl = realized + unrealized
    entries = [_parse_ts(t.get("entry_date")) for t in real]
    entries = sorted(e for e in entries if e is not None)

    return Snapshot(
        ts=now.isoformat(),
        strategy=strategy,
        starting_capital=round(base, 2),
        realized_pnl=round(realized, 2),
        unrealized_pnl=round(unrealized, 2),
        equity=round(base + total_pnl, 2),
        total_pnl=round(total_pnl, 2),
        total_return_pct=(total_pnl / base) if base else 0.0,
        open_count=len(opened),
        open_cost_basis=round(sum(cost_basis(t) for t in opened), 2),
        open_market_value=round(open_market_value, 2),
        closed_count=len(closed),
        wins=wins,
        losses=losses,
        win_rate=(wins / len(closed)) if closed else 0.0,
        gross_profit=round(gross_profit, 2),
        gross_loss=round(gross_loss, 2),
        profit_factor=_profit_factor(gross_profit, gross_loss),
        avg_pnl_pct=(sum(pcts) / len(pcts)) if pcts else 0.0,
        best_trade=round(max((pnl_of(t) for t in closed), default=0.0), 2),
        worst_trade=round(min((pnl_of(t) for t in closed), default=0.0), 2),
        total_deployed=round(total_deployed, 2),
        return_on_deployed=(realized / total_deployed) if total_deployed else 0.0,
        first_trade_at=entries[0].isoformat() if entries else None,
        last_trade_at=entries[-1].isoformat() if entries else None,
        marks_complete=marks_complete,
        broker_confirmed=sum(1 for v in broker_status.values() if v == "confirmed"),
        broker_mismatched=sum(
            1 for v in broker_status.values() if v in ("mismatch", "missing")
        ),
        positions=tuple(positions),
    )


# ── Historical curve ──────────────────────────────────────────────────────────

def realized_equity_curve(
    trades: Iterable[dict], starting_capital: Optional[float] = None
) -> list[dict]:
    """Daily realized-only equity curve, rebuilt from closed trades.

    Used to backfill `balance_history` for the period before snapshotting
    existed. Unrealized P&L is unavailable retroactively (no stored marks), so
    every point here is realized-only and labelled `source='rebuilt'`.
    """
    trades = list(trades)
    base = starting_capital if starting_capital else peak_deployed_capital(trades)

    by_day: dict[str, float] = {}
    counts: dict[str, int] = {}
    for t in trades:
        if not (is_real_trade(t) and is_closed(t)):
            continue
        exited = _parse_ts(t.get("exit_date"))
        if exited is None:
            continue
        day = exited.date().isoformat()
        by_day[day] = by_day.get(day, 0.0) + pnl_of(t)
        counts[day] = counts.get(day, 0) + 1

    curve = []
    running = 0.0
    closed_so_far = 0
    for day in sorted(by_day):
        running += by_day[day]
        closed_so_far += counts[day]
        curve.append({
            "date": day,
            "realized_pnl": round(running, 2),
            "day_pnl": round(by_day[day], 2),
            "equity": round(base + running, 2),
            "closed_trades": closed_so_far,
        })
    return curve
