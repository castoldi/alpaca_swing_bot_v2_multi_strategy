"""Confirm the bot's own open trades against Alpaca — and nothing else.

The Alpaca key is shared with other projects, so a broker position is only
evidence about this bot if the bot can prove it opened it. Proof is the
`client_order_id` prefix (`swingv2-…`) recorded on the trade row at entry.

Scope rules, deliberately narrow:

* Only rows in the local `trades` table with `status='open'` are checked.
* A row is only checked when its `client_order_id` carries our prefix.
* Positions the bot does not track are never inspected, reported, or touched.
* Nothing here places, cancels, or modifies an order. It is read-only —
  reconciliation and exits stay in `bot.py`.

Verdicts (per trade):
    confirmed  — broker position exists with the quantity the DB expects
    mismatch   — position exists, quantity differs from the DB
    missing    — the bot thinks it holds this, the broker has no position
    unverified — the broker could not be read, or the row carries no proof
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

from logger_setup import get_logger

log = get_logger(__name__)

# Must match bot.CLIENT_ORDER_PREFIX. Imported lazily below so this module stays
# importable (and testable) without pulling in the whole live-trading stack.
_DEFAULT_PREFIX = "swingv2"

CONFIRMED = "confirmed"
MISMATCH = "mismatch"
MISSING = "missing"
UNVERIFIED = "unverified"


@dataclass(frozen=True)
class TradeCheck:
    trade_id: int
    ticker: str
    status: str                     # one of the verdicts above
    db_shares: float
    broker_shares: Optional[float]
    mark: Optional[float]           # broker's current price, when readable
    detail: str = ""


def _prefix() -> str:
    try:
        from bot import CLIENT_ORDER_PREFIX
        return CLIENT_ORDER_PREFIX
    except Exception:
        return _DEFAULT_PREFIX


def _owns(trade: dict, prefix: str) -> bool:
    """True only when the trade row carries our correlation id."""
    coid = str(trade.get("client_order_id") or "")
    return bool(coid) and coid.startswith(prefix)


def _float(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _positions_by_symbol(tc) -> Optional[dict[str, Any]]:
    """Every broker position keyed by symbol, or None when unreadable.

    None is distinct from {}: an empty account genuinely holds nothing, while an
    unreadable one proves nothing. Conflating them would report every open trade
    as `missing` during an API outage.
    """
    try:
        getter = getattr(tc, "get_all_positions", None)
        positions = getter() if callable(getter) else []
        return {
            str(getattr(p, "symbol", "") or ""): p for p in (positions or [])
        }
    except Exception as exc:
        log.warning("Broker positions unreadable (%s) — trades stay unverified", exc)
        return None


def check_open_trades(tc, open_trades: list[dict], *, qty_tolerance: float = 1e-6) -> list[TradeCheck]:
    """Verify each bot-owned open trade against the broker's positions.

    Read-only. Returns one verdict per trade, in the order given.
    """
    prefix = _prefix()
    by_symbol = _positions_by_symbol(tc)
    checks: list[TradeCheck] = []

    for trade in open_trades or []:
        trade_id = int(trade.get("id") or 0)
        ticker = str(trade.get("ticker") or "")
        db_shares = _float(trade.get("shares")) or 0.0

        if not _owns(trade, prefix):
            checks.append(TradeCheck(
                trade_id, ticker, UNVERIFIED, db_shares, None, None,
                "no swingv2 correlation id on the trade row",
            ))
            continue

        if by_symbol is None:
            checks.append(TradeCheck(
                trade_id, ticker, UNVERIFIED, db_shares, None, None,
                "broker positions unreadable",
            ))
            continue

        pos = by_symbol.get(ticker)
        if pos is None:
            checks.append(TradeCheck(
                trade_id, ticker, MISSING, db_shares, 0.0, None,
                "broker holds no position for this symbol",
            ))
            continue

        broker_shares = _float(getattr(pos, "qty", None))
        mark = _float(getattr(pos, "current_price", None))
        if broker_shares is None:
            checks.append(TradeCheck(
                trade_id, ticker, UNVERIFIED, db_shares, None, mark,
                "broker quantity unreadable",
            ))
            continue

        broker_shares = abs(broker_shares)
        if abs(broker_shares - db_shares) <= qty_tolerance:
            checks.append(TradeCheck(
                trade_id, ticker, CONFIRMED, db_shares, broker_shares, mark,
            ))
        else:
            # Not an error on its own: the shared key means a sibling bot may
            # hold the same symbol, so the broker total can legitimately exceed
            # what this bot owns. Surfaced for review, never acted on here.
            checks.append(TradeCheck(
                trade_id, ticker, MISMATCH, db_shares, broker_shares, mark,
                f"db {db_shares:g} vs broker {broker_shares:g} shares",
            ))

    return checks


def marks_from_checks(checks: list[TradeCheck]) -> dict[str, float]:
    """Ticker → last price, for the positions the broker could price."""
    return {
        c.ticker: c.mark
        for c in checks
        if c.mark is not None and c.mark > 0
    }


def status_map(checks: list[TradeCheck]) -> dict[int, str]:
    """Trade id → verdict, shaped for `portfolio.build_snapshot`."""
    return {c.trade_id: c.status for c in checks}


def fetch_marks(tickers: list[str]) -> dict[str, float]:
    """Last prices for tickers the broker could not price (e.g. `missing` rows).

    Falls back to the market data feed so a position the broker has already
    liquidated still gets a mark for the snapshot. Never raises.
    """
    if not tickers:
        return {}
    try:
        import data_feed
        snaps = data_feed.fetch_snapshots(sorted(set(tickers))) or {}
    except Exception as exc:
        log.warning("Snapshot marks unavailable (%s)", exc)
        return {}
    out: dict[str, float] = {}
    for ticker, snap in snaps.items():
        price = _float((snap or {}).get("price"))
        if price is not None and price > 0:
            out[str(ticker)] = price
    return out
