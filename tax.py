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

# Net Investment Income Tax (§1411): 3.8% on the lesser of net investment income
# and MAGI above these thresholds. Statutory and NOT inflation-indexed.
NIIT_RATE = 0.038
NIIT_THRESHOLD = {"single": 200_000.0, "married_joint": 250_000.0}

# Ordinary-income brackets. ILLUSTRATIVE DEFAULTS — the rates are stable but the
# thresholds move every year. Verify against the IRS revenue procedure for the
# filing year before relying on any figure derived from these, or override via
# `bracket_table=`.
ORDINARY_BRACKETS = {
    "single": (
        (11_925, 0.10), (48_475, 0.12), (103_350, 0.22), (197_300, 0.24),
        (250_525, 0.32), (626_350, 0.35), (float("inf"), 0.37),
    ),
    "married_joint": (
        (23_850, 0.10), (96_950, 0.12), (206_700, 0.22), (394_600, 0.24),
        (501_050, 0.32), (751_600, 0.35), (float("inf"), 0.37),
    ),
}

# Long-term capital-gain rate breakpoints (taxable income).
LTCG_BRACKETS = {
    "single": ((48_350, 0.00), (533_400, 0.15), (float("inf"), 0.20)),
    "married_joint": ((96_700, 0.00), (600_050, 0.15), (float("inf"), 0.20)),
}

# Quarterly estimated-payment due dates (month, day) for income earned in the
# preceding period. The fourth instalment falls in January of the next year.
ESTIMATED_PAYMENT_DUE = ((4, 15), (6, 15), (9, 15), (1, 15))


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


# ── Symbol identity and asset class ───────────────────────────────────────────

def identical_symbols(
    ticker: str, groups: Iterable[Iterable[str]] | None = None
) -> frozenset:
    """Symbols treated as substantially identical to `ticker`.

    Exact symbol only unless `ticker` appears in a configured group. Nothing is
    inferred: two funds tracking the same index are *not* automatically
    substantially identical, and a leveraged ETF versus its underlying index
    fund is an unsettled question the caller must decide deliberately.
    """
    out = {ticker}
    for group in groups or ():
        members = {str(s) for s in group}
        if ticker in members:
            out |= members
    return frozenset(out)


def is_crypto(ticker: str, crypto_symbols: Iterable[str] | None = None) -> bool:
    return ticker in {str(s) for s in (crypto_symbols or ())}


def wash_sale_applies(
    ticker: str,
    mtm_475f: bool = False,
    crypto_symbols: Iterable[str] | None = None,
) -> bool:
    """Whether §1091 reaches this position at all.

    Two carve-outs: a valid §475(f) mark-to-market election exempts the elected
    trading activity entirely, and digital assets are property rather than
    "stocks or securities" so the rule does not currently reach them. Both still
    produce fully tracked gains and losses — only the disallowance is skipped.
    """
    if mtm_475f:
        return False
    return not is_crypto(ticker, crypto_symbols)


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
    disallowed_loss: float = 0.0   # dollars of loss deferred OUT of this sale
    basis_adjustment: float = 0.0  # dollars of others' deferrals received INTO
                                   # this lot's basis (reduces its later gain)
    replacement_trade_id: Optional[int] = None
    deductible_pnl: float = 0.0    # realized_pnl with any disallowance removed
    straddles_year_end: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


# ── Core computation ──────────────────────────────────────────────────────────

def compute_tax_records(
    trades: Iterable[dict],
    *,
    mtm_475f: bool = False,
    identical_groups: Iterable[Iterable[str]] | None = None,
    crypto_symbols: Iterable[str] | None = None,
) -> list[TaxRecord]:
    """Tax treatment for every closed trade, including wash-sale linkage.

    `trades` should be every trade for the account — open ones included, since an
    open position can be the *replacement* purchase that disallows an earlier
    loss.

    Under a §475(f) election no loss is ever disallowed and every position is
    marked ordinary; crypto symbols are tracked but exempt from §1091.
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
        # A §475(f) election converts everything to ordinary income, so the
        # short/long distinction stops existing for elected activity.
        if mtm_475f:
            term = "ordinary"
        else:
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

        # Only LOSSES can be washed, and only where §1091 reaches at all.
        if realized < 0 and wash_sale_applies(rec.ticker, mtm_475f, crypto_symbols):
            replacement = _find_replacement(
                sold_at, rec.ticker, rec.trade_id, shares, purchases,
                identical=identical_symbols(rec.ticker, identical_groups),
            )
            if replacement is not None:
                rep_trade, matched_qty = replacement
                fraction = min(1.0, matched_qty / shares) if shares else 0.0
                disallowed = abs(realized) * fraction
                rec.is_wash_sale = True
                rec.disallowed_loss = round(disallowed, 4)
                rec.replacement_trade_id = rep_trade.get("id")
                rec.deductible_pnl = round(realized + disallowed, 4)
                rec.straddles_year_end = _straddles_year_end(sold_at, rep_trade)

        records.append(rec)

    _return_deferred_losses(records)
    return records


def _return_deferred_losses(records: list[TaxRecord]) -> None:
    """Credit each disallowed loss back to its replacement lot.

    A wash sale defers a loss, it does not destroy it: the disallowed amount is
    added to the replacement shares' cost basis, so when *those* shares are sold
    the gain is smaller (or the loss larger) by exactly that amount.

    Without this the deferral is booked and never recovered, which overstates
    taxable income by the disallowed total and can report tax exceeding profit.
    When the replacement has no record of its own it is still open, so the
    deferral correctly carries past the end of the data instead.
    """
    by_id = {r.trade_id: r for r in records}
    for rec in records:
        if not rec.is_wash_sale or rec.replacement_trade_id is None:
            continue
        target = by_id.get(rec.replacement_trade_id)
        if target is None:
            continue  # replacement still open — deferral genuinely carries
        target.basis_adjustment += rec.disallowed_loss
        target.deductible_pnl = round(
            target.deductible_pnl - rec.disallowed_loss, 4
        )


def _find_replacement(
    sold_at: datetime,
    ticker: str,
    trade_id: int,
    sold_qty: float,
    purchases: list,
    identical: frozenset | None = None,
) -> Optional[tuple[dict, float]]:
    """Earliest qualifying purchase inside the 61-day window, or None.

    Both directions count: a purchase up to 30 days *before* the loss sale
    disallows it just as one made after. `identical` is the set of symbols
    treated as substantially identical — exact symbol only unless configured.
    """
    matches = identical if identical is not None else frozenset({ticker})
    low = sold_at - timedelta(days=WASH_SALE_DAYS)
    high = sold_at + timedelta(days=WASH_SALE_DAYS)
    best: Optional[tuple[datetime, dict, float]] = None
    for bought, sym, qty, pid, trade in purchases:
        if sym not in matches or pid == trade_id:
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


# ── Lot ledger ────────────────────────────────────────────────────────────────

@dataclass
class Lot:
    """One acquisition, consumed as shares are sold."""
    lot_id: str
    trade_id: Optional[int]
    ticker: str
    acquired: Optional[str]
    quantity: float
    remaining: float
    cost_per_share: float
    basis_adjustment: float = 0.0   # wash-sale deferral rolled into this lot
    closed: bool = False

    @property
    def adjusted_cost_per_share(self) -> float:
        if self.quantity <= 0:
            return self.cost_per_share
        return self.cost_per_share + self.basis_adjustment / self.quantity

    def as_dict(self) -> dict:
        d = asdict(self)
        d["adjusted_cost_per_share"] = round(self.adjusted_cost_per_share, 6)
        return d


@dataclass
class Disposal:
    """One sale, matched against one or more lots."""
    trade_id: Optional[int]
    ticker: str
    sold: Optional[str]
    quantity: float
    proceeds: float
    cost_basis: float
    realized_pnl: float
    lot_ids: tuple


def _lot_sort_key(method: str):
    if method == "lifo":
        return lambda lot: (lot.acquired or "", lot.lot_id), True
    # "specific" without an explicit designation falls back to FIFO, which is
    # also what the IRS applies when no identification is made at sale time.
    return lambda lot: (lot.acquired or "", lot.lot_id), False


def build_lot_ledger(
    trades: Iterable[dict],
    method: str = "fifo",
    specific_lots: Optional[dict] = None,
) -> tuple[list[Lot], list[Disposal]]:
    """Acquisition/disposal ledger with per-lot basis and partial consumption.

    Each entry opens a lot; each exit consumes lots under `method` (fifo, lifo,
    or specific). `specific_lots` maps a trade id to an ordered list of lot ids
    to relieve first, which is how a real specific-identification election is
    expressed. Anything it does not cover falls back to FIFO.

    The bot currently opens one whole-share position per ticker and exits it in
    full, so in practice one trade is one lot. This exists so that stops being
    an assumption: partial fills already occur (see `trade_exit_fills`) and
    would otherwise silently corrupt basis.
    """
    events: list[tuple] = []
    for t in trades or []:
        entry = parse_dt(t.get("entry_date"))
        try:
            qty = float(t.get("shares") or 0)
        except (TypeError, ValueError):
            continue
        if entry is None or qty <= 0:
            continue
        price = _cost_basis_price(t)
        if price is None:
            continue
        events.append((entry, 0, t.get("id"), t, qty, price))
        if str(t.get("status") or "") == "closed":
            sold = parse_dt(t.get("exit_date"))
            try:
                exit_px = float(t.get("exit_price") or 0)
            except (TypeError, ValueError):
                exit_px = 0.0
            if sold is not None:
                events.append((sold, 1, t.get("id"), t, qty, exit_px))

    events.sort(key=lambda e: (e[0], e[1], e[2] if e[2] is not None else 0))

    open_lots: dict[str, list[Lot]] = {}
    all_lots: list[Lot] = []
    disposals: list[Disposal] = []
    key_fn, newest_first = _lot_sort_key(method)

    for when, kind, tid, trade, qty, price in events:
        ticker = str(trade.get("ticker") or "")
        if kind == 0:
            lot = Lot(
                lot_id=f"L{tid}", trade_id=tid, ticker=ticker,
                acquired=when.isoformat(), quantity=qty, remaining=qty,
                cost_per_share=price,
            )
            open_lots.setdefault(ticker, []).append(lot)
            all_lots.append(lot)
            continue

        # Disposal: relieve lots in the configured order.
        pool = [l for l in open_lots.get(ticker, []) if l.remaining > 1e-9]
        preferred = (specific_lots or {}).get(tid) or []
        ordered = [l for pid in preferred for l in pool if l.lot_id == pid]
        ordered += sorted(
            [l for l in pool if l not in ordered], key=key_fn, reverse=newest_first
        )

        need = qty
        basis = 0.0
        used: list[str] = []
        for lot in ordered:
            if need <= 1e-9:
                break
            take = min(need, lot.remaining)
            basis += take * lot.adjusted_cost_per_share
            lot.remaining -= take
            if lot.remaining <= 1e-9:
                lot.closed = True
            need -= take
            used.append(lot.lot_id)

        matched = qty - need
        proceeds = matched * price
        disposals.append(Disposal(
            trade_id=tid, ticker=ticker, sold=when.isoformat(),
            quantity=round(matched, 6), proceeds=round(proceeds, 4),
            cost_basis=round(basis, 4), realized_pnl=round(proceeds - basis, 4),
            lot_ids=tuple(used),
        ))

    return all_lots, disposals


def apply_wash_basis_adjustments(
    lots: list[Lot], records: Iterable[TaxRecord]
) -> list[Lot]:
    """Roll each disallowed loss into its replacement lot's basis.

    This is what makes a wash sale a deferral rather than a loss: the amount
    disallowed on the sale increases what the replacement shares cost, and comes
    back when those shares are sold.
    """
    by_trade = {lot.trade_id: lot for lot in lots}
    for rec in records:
        if not rec.is_wash_sale or rec.replacement_trade_id is None:
            continue
        target = by_trade.get(rec.replacement_trade_id)
        if target is not None:
            target.basis_adjustment += rec.disallowed_loss
    return lots


# ── Year summary / forecast ───────────────────────────────────────────────────

def summarize_year(
    records: Iterable[TaxRecord],
    year: int,
    short_term_rate: float,
    long_term_rate: float,
    *,
    use_brackets: bool = False,
    filing_status: str = "single",
    other_income: float = 0.0,
    apply_niit: bool = True,
    estimated_payments: bool = False,
    mtm_475f: bool = False,
) -> dict:
    """Realized totals and an estimated liability for one tax year.

    With `use_brackets` the estimate uses progressive ordinary brackets, LTCG
    breakpoints and NIIT instead of the two flat rates; the flat path is kept as
    the default so existing figures do not move without opting in.
    """
    rows = []
    for r in records:
        sold = parse_dt(r.sale_date)
        if sold is not None and sold.year == year:
            rows.append(r)

    # Under §475(f) everything is ordinary, so it lands in the short bucket,
    # which is already taxed at ordinary rates.
    short_gain = sum(
        r.deductible_pnl for r in rows if r.term in ("short", "ordinary")
    )
    long_gain = sum(r.deductible_pnl for r in rows if r.term == "long")
    gross = sum(r.realized_pnl for r in rows)
    disallowed = sum(r.disallowed_loss for r in rows)
    wash_count = sum(1 for r in rows if r.is_wash_sale)
    straddling = [r for r in rows if r.straddles_year_end]

    net = short_gain + long_gain
    detail: dict = {}
    if use_brackets:
        detail = bracket_liability(
            short_gain, long_gain, filing_status=filing_status,
            other_income=other_income, apply_niit=apply_niit,
        )
        tax = detail["total_tax"]
        deductible_now = detail["loss_deductible_this_year"]
        carryforward = detail["loss_carryforward"]
    elif net >= 0:
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

    # A §475(f) election removes the capital-loss limitation entirely: losses
    # are ordinary and fully deductible against other income.
    if mtm_475f and net < 0:
        deductible_now = abs(net)
        carryforward = 0.0

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
        "mtm_475f": mtm_475f,
        "uses_brackets": use_brackets,
        "bracket_detail": detail,
        "estimated_payments": (
            estimated_payment_schedule(tax, year) if estimated_payments else []
        ),
    }


def _progressive_tax(amount: float, other_income: float, table) -> float:
    """Tax on `amount` stacked on top of `other_income`, marginally."""
    if amount <= 0:
        return 0.0
    total = 0.0
    lower = other_income
    upper = other_income + amount
    prev_cap = 0.0
    for cap, rate in table:
        band_lo, band_hi = max(prev_cap, lower), min(cap, upper)
        if band_hi > band_lo:
            total += (band_hi - band_lo) * rate
        prev_cap = cap
        if cap >= upper:
            break
    return total


def bracket_liability(
    short_gain: float,
    long_gain: float,
    *,
    filing_status: str = "single",
    other_income: float = 0.0,
    apply_niit: bool = True,
    bracket_table=None,
    ltcg_table=None,
) -> dict:
    """Progressive federal estimate: ordinary brackets, LTCG rates, and NIIT.

    Short-term gains stack on ordinary income; long-term gains are taxed at the
    preferential rates above them. NIIT (§1411) adds 3.8% on the lesser of net
    investment income and MAGI over the statutory threshold.

    Thresholds in the default tables are illustrative — see ORDINARY_BRACKETS.
    """
    status = filing_status if filing_status in ORDINARY_BRACKETS else "single"
    ordinary = bracket_table or ORDINARY_BRACKETS[status]
    ltcg = ltcg_table or LTCG_BRACKETS[status]

    net = short_gain + long_gain
    if net < 0:
        deductible = min(CAPITAL_LOSS_ANNUAL_LIMIT, abs(net))
        return {
            "ordinary_tax": 0.0, "ltcg_tax": 0.0, "niit": 0.0,
            "total_tax": 0.0, "effective_rate": 0.0,
            "loss_deductible_this_year": round(deductible, 2),
            "loss_carryforward": round(abs(net) - deductible, 2),
        }

    # A loss in one bucket offsets the other before any rate is applied.
    st = short_gain
    lt = long_gain
    if st < 0:
        lt, st = max(0.0, lt + st), 0.0
    if lt < 0:
        st, lt = max(0.0, st + lt), 0.0

    ordinary_tax = _progressive_tax(st, other_income, ordinary)
    ltcg_tax = _progressive_tax(lt, other_income + st, ltcg)

    niit = 0.0
    if apply_niit:
        magi = other_income + st + lt
        threshold = NIIT_THRESHOLD.get(status, NIIT_THRESHOLD["single"])
        over = max(0.0, magi - threshold)
        niit = NIIT_RATE * min(max(0.0, st + lt), over)

    total = ordinary_tax + ltcg_tax + niit
    return {
        "ordinary_tax": round(ordinary_tax, 2),
        "ltcg_tax": round(ltcg_tax, 2),
        "niit": round(niit, 2),
        "total_tax": round(total, 2),
        "effective_rate": round(total / net, 4) if net > 0 else 0.0,
        "loss_deductible_this_year": 0.0,
        "loss_carryforward": 0.0,
    }


def estimated_payment_schedule(total_tax: float, year: int) -> list[dict]:
    """Four equal instalments against the year's liability.

    A simple safe-harbour view, not a period-by-period annualisation: it splits
    the liability evenly across the statutory due dates, the last falling in
    January of the following year.
    """
    if total_tax <= 0:
        return []
    each = round(total_tax / 4.0, 2)
    out = []
    for index, (month, day) in enumerate(ESTIMATED_PAYMENT_DUE, start=1):
        due_year = year + 1 if month == 1 else year
        out.append({
            "quarter": f"Q{index}",
            "due": f"{due_year}-{month:02d}-{day:02d}",
            "amount": each,
        })
    return out


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
    hard_block: bool = False,
    hard_block_days: int = 31,
    mtm_475f: bool = False,
    crypto_symbols: Iterable[str] | None = None,
) -> Optional[str]:
    """Reason to skip an entry for tax purposes, or None to allow it.

    Deliberately narrow. Blocking every re-entry within 30 days of a loss all
    year would idle most of a 5-ticker universe — measured on this bot's own
    history, 100% of losing trades already sit inside a 61-day window. Intra-year
    that costs nothing real, because the disallowed loss rides along in the
    replacement lot's basis and is recovered on the next sale within the same
    tax year.

    The guard therefore runs on **both sides of the year boundary**:

    * from `guard_start_*` (default 1 December) to year end, where a fresh wash
      sale would leave a replacement open across 31 December; and
    * into the new year for as long as a loss realised in the *previous* tax
      year is still inside its 30-day replacement window.

    The second half is not optional. A loss taken on 20 December is still washed
    by a repurchase on 10 January — that is 21 days later, inside the window —
    and buying then disallows a prior-year deduction and carries it forward.
    Safe re-entry is 31 days after the loss sale, which for a late-December loss
    falls in the following January.
    """
    if not enabled:
        return None
    # Nothing to defend when §1091 does not reach the position in the first
    # place: an elected trader is exempt, and crypto is property, not a security.
    if not wash_sale_applies(ticker, mtm_475f, crypto_symbols):
        return None

    now = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)

    # Conservative mode: refuse every entry for a flat window centred on 31
    # December, regardless of whether this ticker has a loss. Staying wholly out
    # of the market for 31 consecutive days spanning year end is the standard
    # way an active trader sidesteps §1091 rather than merely tracking it.
    if hard_block:
        span = max(1, int(hard_block_days))
        half = span // 2
        for boundary_year in (now.year - 1, now.year):
            year_end = datetime(boundary_year, 12, 31, tzinfo=timezone.utc)
            start = year_end - timedelta(days=half)
            end = start + timedelta(days=span)
            if start <= now < end:
                return (
                    f"wash-sale hard block: {span}-day flat window "
                    f"{start.date()} to {end.date()} spanning "
                    f"{boundary_year}-12-31 — no entries taken"
                )

    guard_start = datetime(
        now.year, guard_start_month, guard_start_day, tzinfo=timezone.utc
    )
    in_year_end_window = now >= guard_start

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
        if pnl >= 0:
            continue

        # A trailing loss booked in an earlier tax year is always guard-relevant:
        # re-entering now disallows a deduction that has already been counted
        # against last year and rolls it forward.
        crosses_year_boundary = sold.year < now.year
        if not (in_year_end_window or crosses_year_boundary):
            continue

        days_clear = WASH_SALE_DAYS + 1 - (now - sold).days
        whose_year = (
            f"the {sold.year} tax year" if crosses_year_boundary else "next tax year"
        )
        return (
            f"wash-sale year-end guard: {ticker} realised a "
            f"${abs(pnl):.2f} loss on {sold.date()}; re-entering now would "
            f"defer that loss out of {whose_year} "
            f"(safe re-entry in ~{max(0, days_clear)}d)"
        )
    return None
