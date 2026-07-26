"""Tax accounting for closed trades: wash sales, term classification, forecast.

Scope and correctness notes
---------------------------
The wash-sale rule (IRC §1091) disallows a loss when substantially identical
securities are acquired within a **61-day window centred on the loss sale**:
the 30 days before, the sale day, and the 30 days after. It applies year-round
to every loss sale — it is *not* a calendar window around year end, and the
backward-looking half catches purchases made *before* the sale.

A disallowed loss is not destroyed. It is added to the cost basis of the
replacement shares, so it is recovered on the next sale. For a bot that trades
continuously and is flat at year end, intra-year wash sales therefore largely
cancel out. The case that actually costs money is a wash sale whose replacement
position is **still open on 31 December** — that shifts the deduction into the
following tax year. That is the case `year_end_entry_block` guards.

This module is deliberately pure: it takes trade dicts and returns results, with
no database or broker access, so every rule here is directly testable.

**Not tax advice.** These are mechanical computations of published rules. Basis
tracking across partial fills, corporate actions, and "substantially identical"
judgements are areas where a CPA should review before anything here is relied on
for a real filing. See docs/tax-awareness.md.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

# The statutory window: 30 days either side of the sale, plus the sale day.
WASH_SALE_DAYS = 30
WASH_SALE_WINDOW_DAYS = 2 * WASH_SALE_DAYS + 1  # 61

# Long-term capital gains require holding MORE than one year.
LONG_TERM_DAYS = 365

# An individual may deduct at most this much net capital loss against ordinary
# income per year; the remainder carries forward.
CAPITAL_LOSS_ANNUAL_LIMIT = 3000.0


# ── Date handling ─────────────────────────────────────────────────────────────

def parse_dt(value: Any) -> Optional[datetime]:
    """Parse the several timestamp shapes the trades table holds.

    Rows written by different code paths carry `2026-07-02 16:00:00`, full ISO
    with an offset, or a `Z` suffix. Everything is normalised to UTC so window
    arithmetic is comparable.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        text = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _cost_basis_price(trade: dict) -> Optional[float]:
    """Broker fill price when known, else the signal entry price."""
    for key in ("entry_filled_price", "entry_price"):
        raw = trade.get(key)
        if raw is None:
            continue
        try:
            price = float(raw)
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    return None


# ── Result type ───────────────────────────────────────────────────────────────

@dataclass
class TaxRecord:
    trade_id: int
    ticker: str
    sale_date: Optional[str]
    shares: float
    cost_basis: float
    proceeds: float
    realized_pnl: float
    holding_days: float
    term: str                      # 'short' | 'long'
    is_wash_sale: bool = False
    disallowed_loss: float = 0.0   # positive dollars of loss deferred
    basis_adjustment: float = 0.0  # added to the replacement lot's basis
    replacement_trade_id: Optional[int] = None
    deductible_pnl: float = 0.0    # realized_pnl with any disallowance removed
    straddles_year_end: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


# ── Core computation ──────────────────────────────────────────────────────────

def compute_tax_records(trades: Iterable[dict]) -> list[TaxRecord]:
    """Tax treatment for every closed trade, including wash-sale linkage.

    `trades` should be every trade for the account — open ones included, since an
    open position can be the *replacement* purchase that disallows an earlier
    loss.
    """
    all_trades = [t for t in trades if t is not None]

    # Purchases are candidate replacements, whether or not they have closed.
    purchases = []
    for t in all_trades:
        bought = parse_dt(t.get("entry_date"))
        if bought is None:
            continue
        try:
            qty = float(t.get("shares") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 0:
            continue
        purchases.append((bought, str(t.get("ticker") or ""), qty, t.get("id"), t))

    records: list[TaxRecord] = []
    for trade in all_trades:
        if str(trade.get("status") or "") != "closed":
            continue
        sold_at = parse_dt(trade.get("exit_date"))
        bought_at = parse_dt(trade.get("entry_date"))
        entry_price = _cost_basis_price(trade)
        try:
            shares = float(trade.get("shares") or 0)
            exit_price = float(trade.get("exit_price") or 0)
        except (TypeError, ValueError):
            continue
        if sold_at is None or entry_price is None or shares <= 0:
            continue

        cost_basis = shares * entry_price
        proceeds = shares * exit_price
        realized = proceeds - cost_basis
        holding_days = (
            (sold_at - bought_at).total_seconds() / 86400.0 if bought_at else 0.0
        )
        term = "long" if holding_days > LONG_TERM_DAYS else "short"

        rec = TaxRecord(
            trade_id=int(trade.get("id") or 0),
            ticker=str(trade.get("ticker") or ""),
            sale_date=sold_at.isoformat(),
            shares=shares,
            cost_basis=round(cost_basis, 4),
            proceeds=round(proceeds, 4),
            realized_pnl=round(realized, 4),
            holding_days=round(holding_days, 4),
            term=term,
            deductible_pnl=round(realized, 4),
        )

        # Only LOSSES can be washed. Gains are always taxable when realized.
        if realized < 0:
            replacement = _find_replacement(
                sold_at, rec.ticker, rec.trade_id, shares, purchases
            )
            if replacement is not None:
                rep_trade, matched_qty = replacement
                fraction = min(1.0, matched_qty / shares) if shares else 0.0
                disallowed = abs(realized) * fraction
                rec.is_wash_sale = True
                rec.disallowed_loss = round(disallowed, 4)
                rec.basis_adjustment = round(disallowed, 4)
                rec.replacement_trade_id = rep_trade.get("id")
                rec.deductible_pnl = round(realized + disallowed, 4)
                rec.straddles_year_end = _straddles_year_end(sold_at, rep_trade)

        records.append(rec)

    return records


def _find_replacement(
    sold_at: datetime,
    ticker: str,
    trade_id: int,
    sold_qty: float,
    purchases: list,
) -> Optional[tuple[dict, float]]:
    """Earliest qualifying purchase inside the 61-day window, or None.

    Both directions count: a purchase up to 30 days *before* the loss sale
    disallows it just as one made after.
    """
    low = sold_at - timedelta(days=WASH_SALE_DAYS)
    high = sold_at + timedelta(days=WASH_SALE_DAYS)
    best: Optional[tuple[datetime, dict, float]] = None
    for bought, sym, qty, pid, trade in purchases:
        if sym != ticker or pid == trade_id:
            continue
        if not (low <= bought <= high):
            continue
        if best is None or bought < best[0]:
            best = (bought, trade, qty)
    if best is None:
        return None
    return best[1], best[2]


def _straddles_year_end(sold_at: datetime, replacement: dict) -> bool:
    """True when the replacement position is still open at 31 December.

    This is the case that actually moves a deduction between tax years, rather
    than merely deferring it to the next trade inside the same year.
    """
    year_end = datetime(sold_at.year, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    rep_exit = parse_dt(replacement.get("exit_date"))
    rep_entry = parse_dt(replacement.get("entry_date"))
    if rep_entry is None or rep_entry > year_end:
        return False
    if str(replacement.get("status") or "") == "open":
        return True
    return rep_exit is not None and rep_exit > year_end


# ── Year summary / forecast ───────────────────────────────────────────────────

def summarize_year(
    records: Iterable[TaxRecord],
    year: int,
    short_term_rate: float,
    long_term_rate: float,
) -> dict:
    """Realized totals and an estimated liability for one tax year."""
    rows = []
    for r in records:
        sold = parse_dt(r.sale_date)
        if sold is not None and sold.year == year:
            rows.append(r)

    short_gain = sum(r.deductible_pnl for r in rows if r.term == "short")
    long_gain = sum(r.deductible_pnl for r in rows if r.term == "long")
    gross = sum(r.realized_pnl for r in rows)
    disallowed = sum(r.disallowed_loss for r in rows)
    wash_count = sum(1 for r in rows if r.is_wash_sale)
    straddling = [r for r in rows if r.straddles_year_end]

    net = short_gain + long_gain
    if net >= 0:
        tax = max(0.0, short_gain) * short_term_rate + max(0.0, long_gain) * long_term_rate
        # A loss in one bucket offsets a gain in the other before tax applies.
        if short_gain < 0 or long_gain < 0:
            tax = _blended_tax(short_gain, long_gain, short_term_rate, long_term_rate)
        deductible_now = 0.0
        carryforward = 0.0
    else:
        tax = 0.0
        deductible_now = min(CAPITAL_LOSS_ANNUAL_LIMIT, abs(net))
        carryforward = abs(net) - deductible_now

    return {
        "year": year,
        "trades_closed": len(rows),
        "gross_realized_pnl": round(gross, 2),
        "wash_sale_count": wash_count,
        "disallowed_loss_total": round(disallowed, 2),
        "short_term_net": round(short_gain, 2),
        "long_term_net": round(long_gain, 2),
        "net_capital_gain": round(net, 2),
        "estimated_tax": round(tax, 2),
        "loss_deductible_this_year": round(deductible_now, 2),
        "loss_carryforward": round(carryforward, 2),
        "short_term_rate": short_term_rate,
        "long_term_rate": long_term_rate,
        "year_end_straddle_count": len(straddling),
        "year_end_straddle_tickers": sorted({r.ticker for r in straddling}),
    }


def _blended_tax(short_gain, long_gain, st_rate, lt_rate) -> float:
    """Tax after netting a loss in one bucket against a gain in the other."""
    if short_gain < 0 and long_gain > 0:
        remaining = max(0.0, long_gain + short_gain)
        return remaining * lt_rate
    if long_gain < 0 and short_gain > 0:
        remaining = max(0.0, short_gain + long_gain)
        return remaining * st_rate
    return max(0.0, short_gain) * st_rate + max(0.0, long_gain) * lt_rate


# ── Entry guard ───────────────────────────────────────────────────────────────

def year_end_entry_block(
    ticker: str,
    now: datetime,
    trades: Iterable[dict],
    guard_start_month: int = 12,
    guard_start_day: int = 1,
    enabled: bool = True,
) -> Optional[str]:
    """Reason to skip an entry for tax purposes, or None to allow it.

    Deliberately narrow. Blocking every re-entry within 30 days of a loss all
    year would idle most of a 5-ticker universe — measured on this bot's own
    history, 100% of losing trades already sit inside a 61-day window. Intra-year
    that costs nothing real, because the disallowed loss rides along in the
    replacement lot's basis and is recovered on the next sale within the same
    tax year.

    The guard therefore only runs from `guard_start_*` (default 1 December)
    through year end, where a fresh wash sale would leave a replacement position
    open across 31 December and push the deduction into the next tax year. This
    is the standard practice for active traders: be clear of a loss ticker for
    31 days spanning year end.
    """
    if not enabled:
        return None
    now = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    guard_start = datetime(
        now.year, guard_start_month, guard_start_day, tzinfo=timezone.utc
    )
    if now < guard_start:
        return None

    cutoff = now - timedelta(days=WASH_SALE_DAYS)
    for trade in trades or []:
        if str(trade.get("ticker") or "") != ticker:
            continue
        if str(trade.get("status") or "") != "closed":
            continue
        sold = parse_dt(trade.get("exit_date"))
        if sold is None or sold < cutoff or sold > now:
            continue
        try:
            pnl = float(trade.get("pnl_dollars") or 0)
        except (TypeError, ValueError):
            continue
        if pnl < 0:
            days_clear = WASH_SALE_DAYS + 1 - (now - sold).days
            return (
                f"wash-sale year-end guard: {ticker} realised a "
                f"${abs(pnl):.2f} loss on {sold.date()}; re-entering now would "
                f"defer that loss into next tax year "
                f"(clear in ~{max(0, days_clear)}d)"
            )
    return None
