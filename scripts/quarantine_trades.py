"""Move corrupt live-ledger rows from `trades` to `trades_quarantine`.

A row is a candidate when it is a closed, non-zero-share trade that either has
no confirmed entry fill (`entry_filled_price` NULL) or shares its exit order id
with another trade. Every candidate is checked against Alpaca's order history
and printed with the evidence; nothing moves without ``--apply``, and the
database is backed up first. Rows are moved, never deleted.

Usage:
    python scripts/quarantine_trades.py            # dry run: list + evidence
    python scripts/quarantine_trades.py --apply    # back up, then move
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dashboard import db as db_mod  # noqa: E402

REASON = ("corrupt ledger row: no confirmed entry fill and/or exit order claimed by "
          "another trade or not placed by this bot (live-readiness 0.1)")


def candidates() -> list[dict]:
    db_mod._ensure_tables()
    with db_mod._con() as c:
        rows = c.execute(
            """
            SELECT * FROM trades t
            WHERE status='closed' AND COALESCE(shares, 0) > 0 AND (
                entry_filled_price IS NULL
                OR (exit_alpaca_order_id IS NOT NULL AND EXISTS (
                    SELECT 1 FROM trades o WHERE o.id != t.id
                    AND o.exit_alpaca_order_id = t.exit_alpaca_order_id)))
            ORDER BY id
            """
        ).fetchall()
    return [dict(r) for r in rows]


def broker_evidence(rows: list[dict]) -> dict[int, str]:
    """Trade id -> one-line verdict from Alpaca order history ('' if unreachable)."""
    try:
        from alpaca.trading.client import TradingClient
        from config import ALPACA_KEY, ALPACA_SECRET
        tc = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)
    except Exception as exc:
        print(f"Broker unavailable ({exc}); evidence skipped")
        return {}
    cache: dict[str, object] = {}

    def order(oid):
        if oid and oid not in cache:
            try:
                cache[oid] = tc.get_order_by_id(oid)
            except Exception:
                cache[oid] = None
        return cache.get(oid) if oid else None

    out = {}
    for t in rows:
        entry, exit_ = order(t.get("alpaca_order_id")), order(t.get("exit_alpaca_order_id"))
        filled = entry is not None and getattr(entry, "filled_at", None) is not None
        own = exit_ is not None and str(exit_.client_order_id or "").startswith("swingv2")
        before = (filled and exit_ is not None and exit_.filled_at is not None
                  and exit_.filled_at < entry.filled_at)
        out[t["id"]] = ", ".join([
            "entry filled" if filled else "entry NEVER filled",
            "own exit" if own else "exit NOT this bot's",
        ] + (["exit filled BEFORE entry"] if before else []))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    rows = candidates()
    if not rows:
        print("No corrupt rows found.")
        return 0
    evidence = broker_evidence(rows)
    pnl = sum(float(t.get("pnl_dollars") or 0) for t in rows)
    for t in rows:
        print(f"#{t['id']:>4} {t['ticker']:<5} {str(t['entry_date'])[:16]} "
              f"shares {t['shares']:g} pnl {float(t.get('pnl_dollars') or 0):+8.2f}  "
              f"{evidence.get(t['id'], '')}")
    print(f"{len(rows)} rows, P&L {pnl:+.2f}")
    if not args.apply:
        print("Dry run. Re-run with --apply to move them.")
        return 0

    backup = db_mod._DB.with_name(
        f"{db_mod._DB.stem}.pre-quarantine-{datetime.now():%Y%m%d-%H%M%S}.db")
    with sqlite3.connect(db_mod._DB) as src, sqlite3.connect(backup) as dst:
        src.backup(dst)
    print(f"Backup: {backup}")
    moved = db_mod.quarantine_trades([t["id"] for t in rows], REASON)
    print(f"Moved {moved} rows to trades_quarantine.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
