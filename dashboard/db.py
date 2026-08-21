"""SQLite database for Alpaca Swing Bot V2 — stores trades, signals, runs, experiments."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_DB: Path = Path(__file__).parent / "swing_bot_v2.db"
_TICKERS: list[str] = []


def _con() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_tables():
    """Create tables if they don't exist."""
    with _con() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS bot_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                strategy TEXT NOT NULL,
                status TEXT DEFAULT 'running',
                trades_found INTEGER DEFAULT 0,
                orders_placed INTEGER DEFAULT 0,
                error TEXT,
                deployed REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                strategy TEXT NOT NULL,
                entry_date TEXT NOT NULL,
                entry_price REAL NOT NULL,
                stop_loss REAL NOT NULL,
                take_profit REAL NOT NULL,
                exit_date TEXT,
                exit_price REAL,
                exit_reason TEXT,
                bars_held INTEGER,
                shares REAL,
                pnl_dollars REAL,
                pnl_pct REAL,
                entry_state TEXT DEFAULT 'accepted',
                status TEXT DEFAULT 'open',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                strategy TEXT NOT NULL,
                signal_date TEXT NOT NULL,
                entry_price REAL,
                stop_loss REAL,
                take_profit REAL,
                atr REAL,
                rsi REAL,
                acted BOOLEAN DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS backtest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                year INTEGER NOT NULL,
                strategy TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                num_trades INTEGER DEFAULT 0,
                win_rate REAL DEFAULT 0,
                total_pnl REAL DEFAULT 0,
                profit_factor REAL DEFAULT 0,
                max_drawdown REAL DEFAULT 0,
                sharpe_ratio REAL DEFAULT 0,
                status TEXT DEFAULT 'running'
            );
            CREATE TABLE IF NOT EXISTS research_experiments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                description TEXT NOT NULL,
                changes_made TEXT,
                strategy_tested TEXT,
                result_2025_pnl REAL,
                result_2026_pnl REAL,
                combined_pnl REAL,
                verdict TEXT DEFAULT 'pending'
            );
            CREATE TABLE IF NOT EXISTS tax_records (
                trade_id INTEGER PRIMARY KEY,
                ticker TEXT NOT NULL,
                sale_date TEXT,
                shares REAL NOT NULL DEFAULT 0,
                cost_basis REAL NOT NULL DEFAULT 0,
                proceeds REAL NOT NULL DEFAULT 0,
                realized_pnl REAL NOT NULL DEFAULT 0,
                holding_days REAL NOT NULL DEFAULT 0,
                term TEXT NOT NULL DEFAULT 'short',
                is_wash_sale INTEGER NOT NULL DEFAULT 0,
                disallowed_loss REAL NOT NULL DEFAULT 0,
                basis_adjustment REAL NOT NULL DEFAULT 0,
                replacement_trade_id INTEGER,
                deductible_pnl REAL NOT NULL DEFAULT 0,
                straddles_year_end INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (trade_id) REFERENCES trades(id)
            );
            CREATE TABLE IF NOT EXISTS trade_exit_fills (
                trade_id INTEGER NOT NULL,
                alpaca_order_id TEXT NOT NULL,
                client_order_id TEXT,
                filled_qty REAL NOT NULL DEFAULT 0,
                filled_notional REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (trade_id, alpaca_order_id),
                FOREIGN KEY (trade_id) REFERENCES trades(id)
            );
            -- The bot's own equity curve. Broker equity is shared with other
            -- projects on the same Alpaca key and therefore cannot measure this
            -- bot; every column here is derived from the local trades table.
            -- Dollars are stored, percentages are derived on read, because the
            -- capital base is a running maximum that restates old percentages.
            CREATE TABLE IF NOT EXISTS balance_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                strategy TEXT,
                starting_capital REAL NOT NULL DEFAULT 0,
                realized_pnl REAL NOT NULL DEFAULT 0,
                unrealized_pnl REAL NOT NULL DEFAULT 0,
                equity REAL NOT NULL DEFAULT 0,
                open_positions INTEGER NOT NULL DEFAULT 0,
                open_cost_basis REAL NOT NULL DEFAULT 0,
                open_market_value REAL NOT NULL DEFAULT 0,
                closed_trades INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                marks_complete INTEGER NOT NULL DEFAULT 1,
                broker_confirmed INTEGER NOT NULL DEFAULT 0,
                broker_mismatched INTEGER NOT NULL DEFAULT 0,
                broker_equity REAL,
                source TEXT NOT NULL DEFAULT 'bot_run'
            );
            CREATE INDEX IF NOT EXISTS idx_balance_history_ts
                ON balance_history(ts);
            -- Backtest equity curves, one row per strategy per mark.
            --
            -- Backtests reset to `initial_backtest_equity` every January, so a
            -- multi-year "growth of $1" cannot be read off raw equity. What
            -- chains across years is `year_factor` (equity / that year's
            -- starting equity); the API multiplies completed years together to
            -- build a curve from any chosen start year.
            CREATE TABLE IF NOT EXISTS equity_curves (
                strategy TEXT NOT NULL,
                year INTEGER NOT NULL,
                ts TEXT NOT NULL,
                equity REAL NOT NULL,
                year_factor REAL NOT NULL,
                PRIMARY KEY (strategy, ts)
            );
            CREATE INDEX IF NOT EXISTS idx_equity_curves_lookup
                ON equity_curves(strategy, year, ts);
        """)
        _migrate(c)


def _migrate(c: sqlite3.Connection):
    """Idempotently add columns introduced after the first release.

    Correlation columns tie each DB trade to the live Alpaca order(s) so the bot
    can prove a position is its own before ever closing it.
    """
    have = {row["name"] for row in c.execute("PRAGMA table_info(trades)")}
    add = {
        "client_order_id": "TEXT",       # our correlation id sent to Alpaca on entry
        "alpaca_order_id": "TEXT",       # Alpaca's order UUID for the entry
        "entry_state": "TEXT DEFAULT 'accepted'",  # pending_submission | accepted
        "exit_client_order_id": "TEXT",  # correlation id of the closing order
        "exit_alpaca_order_id": "TEXT",  # Alpaca's order UUID for the exit
        "exit_intent_reason": "TEXT",     # durable intent before protection is canceled
        "entry_filled_price": "REAL",    # broker average fill price of the entry
        # Protection re-armed AFTER entry (the original bracket legs died without
        # filling — e.g. another process on the same Alpaca account canceled them).
        # Those replacement legs are not children of the entry order, so
        # reconciliation cannot reach them via order.legs; these ids are the link.
        "protect_client_order_id": "TEXT",
        "protect_alpaca_order_id": "TEXT",
        # Last broker-confirmation verdict from broker_sync (read-only check
        # that the position the bot believes it holds is really there).
        "broker_status": "TEXT",         # confirmed | mismatch | missing | unverified
        "broker_shares": "REAL",         # quantity the broker reported
        "broker_checked_at": "TEXT",
    }
    for col, decl in add.items():
        if col not in have:
            c.execute(f"ALTER TABLE trades ADD COLUMN {col} {decl}")

    # backtest_runs: tag each run with the candle timeframe it ran on. Existing
    # rows predate the 4h switch, so they default to '1d'.
    bt_have = {row["name"] for row in c.execute("PRAGMA table_info(backtest_runs)")}
    if "timeframe" not in bt_have:
        c.execute("ALTER TABLE backtest_runs ADD COLUMN timeframe TEXT DEFAULT '1d'")


def set_tickers(tickers: list[str]):
    global _TICKERS
    _TICKERS = tickers


# ── Bot runs ──────────────────────────────────────────────────────────────────

def start_bot_run(strategy: str) -> int:
    _ensure_tables()
    with _con() as c:
        cur = c.execute(
            "INSERT INTO bot_runs (started_at, strategy) VALUES (?, ?)",
            (datetime.now(timezone.utc).isoformat(), strategy),
        )
        return cur.lastrowid


def finish_bot_run(run_id: int, trades: int = 0, orders: int = 0, error: Optional[str] = None, deployed: float = 0):
    with _con() as c:
        c.execute(
            "UPDATE bot_runs SET finished_at=?, status=?, trades_found=?, orders_placed=?, error=?, deployed=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), "error" if error else "done", trades, orders, error, deployed, run_id),
        )


def get_recent_runs(limit: int = 50) -> list[dict]:
    _ensure_tables()
    with _con() as c:
        rows = c.execute(
            "SELECT * FROM bot_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Trades ────────────────────────────────────────────────────────────────────

def save_trade(ticker: str, strategy: str, entry_date: str, entry_price: float,
               stop_loss: float, take_profit: float, shares: Optional[float] = None,
               client_order_id: Optional[str] = None,
               alpaca_order_id: Optional[str] = None,
               entry_state: str = "accepted") -> int:
    """Persist a newly opened trade, including its Alpaca correlation ids."""
    _ensure_tables()
    with _con() as c:
        cur = c.execute(
            "INSERT INTO trades (ticker, strategy, entry_date, entry_price, stop_loss, "
            "take_profit, shares, client_order_id, alpaca_order_id, entry_state, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,'open')",
            (ticker, strategy, entry_date, entry_price, stop_loss, take_profit,
             shares, client_order_id, alpaca_order_id, entry_state),
        )
        return cur.lastrowid


def set_entry_order_id(db_id: int, alpaca_order_id: Optional[str]):
    """Attach the broker id after a durable client-id entry intent is accepted."""
    with _con() as c:
        c.execute(
            "UPDATE trades SET alpaca_order_id=?, entry_state='accepted' "
            "WHERE id=? AND status='open'",
            (alpaca_order_id, db_id),
        )


def set_protect_order_ids(
    db_id: int,
    client_order_id: Optional[str],
    alpaca_order_id: Optional[str],
):
    """Record the re-armed protective order that now guards an open position.

    Only ever set on an open trade: once the trade closes the ids are history and
    must not be overwritten by a later sweep.
    """
    with _con() as c:
        c.execute(
            "UPDATE trades SET protect_client_order_id=?, protect_alpaca_order_id=? "
            "WHERE id=? AND status='open'",
            (client_order_id, alpaca_order_id, db_id),
        )


def set_entry_fill(db_id: int, filled_price: float, filled_qty: Optional[float] = None):
    """Record the broker's real average entry fill (and quantity when known).

    The signal-close ``entry_price`` stays untouched for reference; live P&L,
    breakeven checks, and time stops should prefer ``entry_filled_price``.
    """
    with _con() as c:
        if filled_qty and filled_qty > 0:
            c.execute(
                "UPDATE trades SET entry_filled_price=?, shares=? "
                "WHERE id=? AND status='open'",
                (filled_price, filled_qty, db_id),
            )
        else:
            c.execute(
                "UPDATE trades SET entry_filled_price=? WHERE id=? AND status='open'",
                (filled_price, db_id),
            )


def close_trade(db_id: int, exit_date: str, exit_price: float, reason: str,
                bars_held: int, shares: float, pnl_dollars: float, pnl_pct: float,
                exit_client_order_id: Optional[str] = None,
                exit_alpaca_order_id: Optional[str] = None):
    with _con() as c:
        c.execute(
            "UPDATE trades SET exit_date=?, exit_price=?, exit_reason=?, bars_held=?, "
            "shares=?, pnl_dollars=?, pnl_pct=?, exit_client_order_id=?, "
            "exit_alpaca_order_id=?, exit_intent_reason=NULL, status='closed' WHERE id=?",
            (exit_date, exit_price, reason, bars_held, shares, pnl_dollars, pnl_pct,
             exit_client_order_id, exit_alpaca_order_id, db_id),
        )


def set_exit_intent(db_id: int, reason: str, exit_client_order_id: str):
    """Persist why/how to exit before canceling any broker-side protection."""
    with _con() as c:
        c.execute(
            "UPDATE trades SET exit_intent_reason=?, exit_client_order_id=?, "
            "exit_alpaca_order_id=NULL WHERE id=? AND status='open'",
            (reason, exit_client_order_id, db_id),
        )


def set_exit_pending(
    db_id: int,
    exit_client_order_id: Optional[str],
    exit_alpaca_order_id: Optional[str],
):
    """Attach a submitted exit order while leaving the trade open until it fills."""
    with _con() as c:
        c.execute(
            "UPDATE trades SET exit_client_order_id=?, exit_alpaca_order_id=? "
            "WHERE id=? AND status='open'",
            (exit_client_order_id, exit_alpaca_order_id, db_id),
        )


def clear_exit_pending(db_id: int):
    """Clear one terminal order but retain durable intent for a later retry."""
    with _con() as c:
        c.execute(
            "UPDATE trades SET exit_client_order_id=NULL, exit_alpaca_order_id=NULL "
            "WHERE id=? AND status='open'",
            (db_id,),
        )


def record_exit_order_progress(
    db_id: int,
    alpaca_order_id: str,
    client_order_id: Optional[str],
    filled_qty: float,
    filled_notional: float,
):
    """Idempotently retain cumulative fill progress for one broker order."""
    if not alpaca_order_id or filled_qty <= 0:
        return
    with _con() as c:
        c.execute(
            """
            INSERT INTO trade_exit_fills
                (trade_id, alpaca_order_id, client_order_id, filled_qty,
                 filled_notional, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_id, alpaca_order_id) DO UPDATE SET
                client_order_id=COALESCE(excluded.client_order_id, client_order_id),
                filled_notional=CASE
                    WHEN excluded.filled_qty >= filled_qty
                    THEN excluded.filled_notional ELSE filled_notional END,
                filled_qty=MAX(filled_qty, excluded.filled_qty),
                updated_at=excluded.updated_at
            """,
            (
                db_id,
                alpaca_order_id,
                client_order_id,
                float(filled_qty),
                float(filled_notional),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def get_exit_fill_totals(db_id: int) -> tuple[float, float]:
    """Cumulative (shares, notional) across stop and market exit orders."""
    with _con() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(filled_qty), 0) AS qty, "
            "COALESCE(SUM(filled_notional), 0) AS notional "
            "FROM trade_exit_fills WHERE trade_id=?",
            (db_id,),
        ).fetchone()
        return float(row["qty"]), float(row["notional"])


def exit_order_already_used(exit_alpaca_order_id: Optional[str]) -> bool:
    """True if a closed trade already claims this Alpaca order as its exit fill.

    An open trade may carry the id while the order is pending, so open rows must
    not hide their own eventual fill from reconciliation.
    """
    if not exit_alpaca_order_id:
        return False
    with _con() as c:
        row = c.execute(
            "SELECT 1 FROM trades WHERE exit_alpaca_order_id=? AND status='closed' LIMIT 1",
            (exit_alpaca_order_id,),
        ).fetchone()
        return row is not None


def get_open_trades_by_strategy(strategy: str) -> list[dict]:
    """Open trades opened by a given strategy (i.e. by this bot process)."""
    _ensure_tables()
    with _con() as c:
        rows = c.execute(
            "SELECT * FROM trades WHERE status='open' AND strategy=? ORDER BY id",
            (strategy,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_open_trade(ticker: str, strategy: Optional[str] = None) -> Optional[dict]:
    """Most recent open trade for a ticker (optionally scoped to a strategy)."""
    _ensure_tables()
    with _con() as c:
        if strategy:
            row = c.execute(
                "SELECT * FROM trades WHERE status='open' AND ticker=? AND strategy=? "
                "ORDER BY id DESC LIMIT 1", (ticker, strategy),
            ).fetchone()
        else:
            row = c.execute(
                "SELECT * FROM trades WHERE status='open' AND ticker=? "
                "ORDER BY id DESC LIMIT 1", (ticker,),
            ).fetchone()
        return dict(row) if row else None


def get_all_trades(limit: int = 200) -> list[dict]:
    _ensure_tables()
    with _con() as c:
        rows = c.execute("SELECT * FROM trades ORDER BY entry_date DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_open_trades() -> list[dict]:
    _ensure_tables()
    with _con() as c:
        rows = c.execute("SELECT * FROM trades WHERE status='open' ORDER BY entry_date DESC").fetchall()
        return [dict(r) for r in rows]


def get_closed_trades(limit: int = 200) -> list[dict]:
    _ensure_tables()
    with _con() as c:
        rows = c.execute(
            "SELECT * FROM trades WHERE status='closed' ORDER BY entry_date DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_trades_for_ledger() -> list[dict]:
    """Every trade ever recorded, unbounded — the input to all P&L math.

    Deliberately not `get_all_trades`, whose LIMIT would silently truncate the
    history and understate lifetime totals once the bot passes that many trades.
    """
    _ensure_tables()
    with _con() as c:
        rows = c.execute("SELECT * FROM trades ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def set_broker_sync(db_id: int, status: str, broker_shares: Optional[float] = None):
    """Record the latest broker-confirmation verdict for one open trade.

    Scoped to open rows: once a trade closes the verdict is history and must not
    be overwritten by a later sweep that no longer sees the position.
    """
    with _con() as c:
        c.execute(
            "UPDATE trades SET broker_status=?, broker_shares=?, broker_checked_at=? "
            "WHERE id=? AND status='open'",
            (status, broker_shares, datetime.now(timezone.utc).isoformat(), db_id),
        )


# ── Balance history ───────────────────────────────────────────────────────────

def save_balance_snapshot(snapshot: dict, source: str = "bot_run",
                          broker_equity: Optional[float] = None) -> int:
    """Append one point to the bot's own equity curve."""
    _ensure_tables()
    with _con() as c:
        cur = c.execute(
            "INSERT INTO balance_history (ts, strategy, starting_capital, "
            "realized_pnl, unrealized_pnl, equity, open_positions, "
            "open_cost_basis, open_market_value, closed_trades, wins, losses, "
            "marks_complete, broker_confirmed, broker_mismatched, broker_equity, "
            "source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                snapshot["ts"], snapshot.get("strategy"),
                snapshot["starting_capital"], snapshot["realized_pnl"],
                snapshot["unrealized_pnl"], snapshot["equity"],
                snapshot["open_count"], snapshot["open_cost_basis"],
                snapshot["open_market_value"], snapshot["closed_count"],
                snapshot["wins"], snapshot["losses"],
                int(bool(snapshot["marks_complete"])),
                snapshot.get("broker_confirmed", 0),
                snapshot.get("broker_mismatched", 0),
                broker_equity, source,
            ),
        )
        return cur.lastrowid


def get_balance_history(limit: int = 500, since: Optional[str] = None) -> list[dict]:
    """Equity-curve points, oldest first (chart order)."""
    _ensure_tables()
    with _con() as c:
        if since:
            rows = c.execute(
                "SELECT * FROM balance_history WHERE ts >= ? ORDER BY ts LIMIT ?",
                (since, limit),
            ).fetchall()
        else:
            # Newest `limit` rows, then flipped back into chronological order.
            rows = c.execute(
                "SELECT * FROM (SELECT * FROM balance_history ORDER BY ts DESC "
                "LIMIT ?) ORDER BY ts", (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_latest_balance() -> Optional[dict]:
    _ensure_tables()
    with _con() as c:
        row = c.execute(
            "SELECT * FROM balance_history ORDER BY ts DESC, id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def get_daily_balance_history(limit: int = 365) -> list[dict]:
    """One point per calendar day — the last snapshot of each day, oldest first.

    The bot snapshots every loop pass (~18/day), which is far too dense to plot
    a multi-month curve; this is the daily close of the bot's own book.
    """
    _ensure_tables()
    with _con() as c:
        rows = c.execute(
            "SELECT * FROM (SELECT * FROM balance_history WHERE id IN "
            "(SELECT MAX(id) FROM balance_history GROUP BY substr(ts, 1, 10)) "
            "ORDER BY ts DESC LIMIT ?) ORDER BY ts",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def replace_rebuilt_balance_history(points: list[dict], strategy: Optional[str] = None) -> int:
    """Swap in a freshly rebuilt realized-only curve.

    Only rows previously written with source='rebuilt' are removed, so genuine
    live snapshots (which also carry unrealized P&L and broker verdicts) are
    never destroyed by a rebuild.
    """
    _ensure_tables()
    with _con() as c:
        c.execute("DELETE FROM balance_history WHERE source='rebuilt'")
        c.executemany(
            "INSERT INTO balance_history (ts, strategy, starting_capital, "
            "realized_pnl, unrealized_pnl, equity, open_positions, "
            "closed_trades, marks_complete, source) "
            "VALUES (?,?,?,?,?,?,?,?,?, 'rebuilt')",
            [
                (
                    f"{p['date']}T23:59:59+00:00", strategy,
                    # Recover the capital base from the identity
                    # equity = base + realized + unrealized.
                    round(p["equity"] - p["realized_pnl"] - p.get("unrealized_pnl", 0.0), 2),
                    p["realized_pnl"], p.get("unrealized_pnl", 0.0),
                    p["equity"], p.get("open_positions", 0),
                    p.get("closed_trades", 0),
                    # A day that had to mark a position at cost is not a
                    # complete mark; the flag keeps that visible downstream.
                    int(not p.get("partial", False)),
                )
                for p in points
            ],
        )
    return len(points)


# ── Backtest equity curves ────────────────────────────────────────────────────

def save_equity_curve(strategy: str, year: int, points: list[tuple[str, float]],
                      initial_equity: float) -> int:
    """Replace one strategy-year curve. `points` is [(iso_ts, equity), ...]."""
    _ensure_tables()
    if initial_equity <= 0:
        raise ValueError("initial_equity must be positive")
    with _con() as c:
        c.execute("DELETE FROM equity_curves WHERE strategy=? AND year=?",
                  (strategy, year))
        c.executemany(
            "INSERT OR REPLACE INTO equity_curves "
            "(strategy, year, ts, equity, year_factor) VALUES (?,?,?,?,?)",
            [(strategy, year, ts, eq, eq / initial_equity) for ts, eq in points],
        )
    return len(points)


def get_equity_curves(from_year: Optional[int] = None) -> dict[str, list[dict]]:
    """Growth-of-$1 curves per strategy, chained across annual resets.

    Each completed year multiplies into a running factor, so a strategy that
    returned +27% in 2024 and +10% in 2025 reads 1.27 then 1.397 — the compound
    a live account would have seen, which raw per-year equity cannot show.
    """
    _ensure_tables()
    with _con() as c:
        if from_year:
            rows = c.execute(
                "SELECT * FROM equity_curves WHERE year >= ? ORDER BY strategy, ts",
                (from_year,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM equity_curves ORDER BY strategy, ts"
            ).fetchall()

    curves: dict[str, list[dict]] = {}
    carry: dict[str, float] = {}       # compounded factor of completed years
    last_year: dict[str, int] = {}
    last_factor: dict[str, float] = {}
    for r in rows:
        strat, year = r["strategy"], r["year"]
        if strat not in carry:
            carry[strat] = 1.0
            curves[strat] = []
        if last_year.get(strat) is not None and year != last_year[strat]:
            # Year rolled over: bank the finished year's factor.
            carry[strat] *= last_factor.get(strat, 1.0)
        last_year[strat] = year
        last_factor[strat] = r["year_factor"]
        curves[strat].append({
            "ts": r["ts"],
            "year": year,
            "growth": round(carry[strat] * r["year_factor"], 6),
        })
    return curves


def get_equity_curve_years() -> list[int]:
    """Years that actually have curve data, ascending."""
    _ensure_tables()
    with _con() as c:
        return [r[0] for r in c.execute(
            "SELECT DISTINCT year FROM equity_curves ORDER BY year")]


# ── Tax records ───────────────────────────────────────────────────────────────

def save_tax_records(records: list) -> int:
    """Upsert one tax record per closed trade. Returns the number written."""
    _ensure_tables()
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            r.trade_id, r.ticker, r.sale_date, r.shares, r.cost_basis, r.proceeds,
            r.realized_pnl, r.holding_days, r.term, int(r.is_wash_sale),
            r.disallowed_loss, r.basis_adjustment, r.replacement_trade_id,
            r.deductible_pnl, int(r.straddles_year_end), now,
        )
        for r in records
    ]
    if not rows:
        return 0
    with _con() as c:
        c.executemany(
            "INSERT INTO tax_records (trade_id, ticker, sale_date, shares, "
            "cost_basis, proceeds, realized_pnl, holding_days, term, is_wash_sale, "
            "disallowed_loss, basis_adjustment, replacement_trade_id, "
            "deductible_pnl, straddles_year_end, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(trade_id) DO UPDATE SET "
            "ticker=excluded.ticker, sale_date=excluded.sale_date, "
            "shares=excluded.shares, cost_basis=excluded.cost_basis, "
            "proceeds=excluded.proceeds, realized_pnl=excluded.realized_pnl, "
            "holding_days=excluded.holding_days, term=excluded.term, "
            "is_wash_sale=excluded.is_wash_sale, "
            "disallowed_loss=excluded.disallowed_loss, "
            "basis_adjustment=excluded.basis_adjustment, "
            "replacement_trade_id=excluded.replacement_trade_id, "
            "deductible_pnl=excluded.deductible_pnl, "
            "straddles_year_end=excluded.straddles_year_end, "
            "updated_at=excluded.updated_at",
            rows,
        )
    return len(rows)


def get_tax_records(limit: int = 500) -> list[dict]:
    _ensure_tables()
    with _con() as c:
        rows = c.execute(
            "SELECT * FROM tax_records ORDER BY sale_date DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def rebuild_tax_records() -> int:
    """Recompute every tax record from the full trade history.

    Wash-sale status is not a property of one trade in isolation — a later
    purchase can retroactively disallow an earlier loss — so records are always
    recomputed over the whole history rather than patched per close.
    """
    import tax as tax_mod
    from config import PARAMS

    _ensure_tables()
    with _con() as c:
        trades = [dict(r) for r in c.execute("SELECT * FROM trades").fetchall()]
    return save_tax_records(tax_mod.compute_tax_records(
        trades,
        mtm_475f=PARAMS.tax_mtm_475f,
        identical_groups=PARAMS.tax_identical_groups,
        crypto_symbols=PARAMS.tax_crypto_symbols,
    ))


# ── Signals ───────────────────────────────────────────────────────────────────

def save_signal(ticker: str, strategy: str, signal_date: str, entry_price: float,
                stop_loss: float, take_profit: float, atr: float, rsi: float) -> int:
    _ensure_tables()
    with _con() as c:
        cur = c.execute(
            "INSERT INTO signals (ticker, strategy, signal_date, entry_price, stop_loss, take_profit, atr, rsi) VALUES (?,?,?,?,?,?,?,?)",
            (ticker, strategy, signal_date, entry_price, stop_loss, take_profit, atr, rsi),
        )
        return cur.lastrowid


def get_recent_signals(limit: int = 100) -> list[dict]:
    _ensure_tables()
    with _con() as c:
        rows = c.execute(
            "SELECT * FROM signals ORDER BY signal_date DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Backtest runs ─────────────────────────────────────────────────────────────

def start_backtest_run(year: int, strategy: str, timeframe: str = "4h") -> int:
    _ensure_tables()
    with _con() as c:
        cur = c.execute(
            "INSERT INTO backtest_runs (year, strategy, started_at, timeframe) VALUES (?, ?, ?, ?)",
            (year, strategy, datetime.now(timezone.utc).isoformat(), timeframe),
        )
        return cur.lastrowid


def finish_backtest_run(run_id: int, num_trades: int, win_rate: float,
                        total_pnl: float, profit_factor: float,
                        max_drawdown: float, sharpe_ratio: float):
    with _con() as c:
        c.execute(
            "UPDATE backtest_runs SET finished_at=?, num_trades=?, win_rate=?, total_pnl=?, profit_factor=?, max_drawdown=?, sharpe_ratio=?, status='done' WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), num_trades, win_rate, total_pnl, profit_factor, max_drawdown, sharpe_ratio, run_id),
        )


def get_backtest_results(year: Optional[int] = None) -> list[dict]:
    """Latest completed run per (year, strategy) — what the cards/tables show.

    Reruns accumulate as history (see get_backtest_history); this returns only the
    most recent finished run for each strategy/year so the headline numbers reflect
    the current timeframe.
    """
    _ensure_tables()
    with _con() as c:
        base = (
            "SELECT * FROM backtest_runs WHERE status='done' AND id IN "
            "(SELECT MAX(id) FROM backtest_runs WHERE status='done' GROUP BY year, strategy)"
        )
        if year:
            rows = c.execute(base + " AND year=? ORDER BY strategy", (year,)).fetchall()
        else:
            rows = c.execute(base + " ORDER BY year DESC, strategy").fetchall()
        return [dict(r) for r in rows]


def get_backtest_history(limit: int = 200, year: Optional[int] = None) -> list[dict]:
    """Every backtest run, newest first — the full historical log."""
    _ensure_tables()
    with _con() as c:
        if year:
            rows = c.execute(
                "SELECT * FROM backtest_runs WHERE year=? ORDER BY id DESC LIMIT ?",
                (year, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM backtest_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


# ── Research experiments ─────────────────────────────────────────────────────

def log_experiment(description: str, changes: str, strategy: str,
                   pnl_2025: float, pnl_2026: float, verdict: str = "pending"):
    _ensure_tables()
    with _con() as c:
        c.execute(
            "INSERT INTO research_experiments (timestamp, description, changes_made, strategy_tested, result_2025_pnl, result_2026_pnl, combined_pnl, verdict) VALUES (?,?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), description, changes, strategy, pnl_2025, pnl_2026, pnl_2025 + pnl_2026, verdict),
        )


def get_experiments(limit: int = 50) -> list[dict]:
    _ensure_tables()
    with _con() as c:
        rows = c.execute("SELECT * FROM research_experiments ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


# ── Portfolio stats ──────────────────────────────────────────────────────────

def portfolio_stats() -> dict[str, Any]:
    _ensure_tables()
    with _con() as c:
        # `shares > 0` filters out durable intent rows (entry_not_submitted /
        # entry_not_filled): those never became positions, so counting them
        # would dilute the win rate and disagree with portfolio.build_snapshot.
        closed = c.execute("""
            SELECT COUNT(*) as trades,
                   SUM(CASE WHEN pnl_dollars > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN pnl_dollars <= 0 THEN 1 ELSE 0 END) as losses,
                   COALESCE(SUM(pnl_dollars), 0) as total_pnl,
                   COALESCE(AVG(pnl_pct), 0) as avg_pnl_pct,
                   COALESCE(SUM(CASE WHEN pnl_dollars > 0 THEN pnl_dollars ELSE 0 END), 0) as gross_profit,
                   COALESCE(SUM(CASE WHEN pnl_dollars < 0 THEN ABS(pnl_dollars) ELSE 0 END), 0) as gross_loss
            FROM trades WHERE status='closed' AND COALESCE(shares, 0) > 0
        """).fetchone()

        open_count = c.execute(
            "SELECT COUNT(*) FROM trades WHERE status='open' AND COALESCE(shares, 0) > 0"
        ).fetchone()[0]

        total = dict(closed)
        total["open_positions"] = open_count
        total["tickers"] = _TICKERS
        if total["gross_loss"] > 0:
            total["profit_factor"] = round(total["gross_profit"] / total["gross_loss"], 2)
        else:
            total["profit_factor"] = total["gross_profit"] if total["gross_profit"] > 0 else 0
        if total["trades"] > 0:
            total["win_rate"] = round(total["wins"] / total["trades"] * 100, 1)
        else:
            total["win_rate"] = 0
        return {k: round(v, 2) if isinstance(v, float) else v for k, v in total.items()}


# ── Init ─────────────────────────────────────────────────────────────────────

# ── Alpaca position sync ─────────────────────────────────────────────────────

def sync_positions_from_alpaca(trading_client) -> dict:
    """Fetch live positions from Alpaca and update the DB.
    
    Returns dict with positions list and total deployed capital.
    """
    _ensure_tables()
    try:
        positions = trading_client.get_all_positions()
        deployed = sum(float(p.market_value) for p in positions) if positions else 0.0
        
        pos_list = []
        for p in (positions or []):
            pos_list.append({
                "ticker": p.symbol,
                "qty": float(p.qty),
                "avg_entry": float(p.avg_entry_price),
                "current_price": float(p.current_price),
                "market_value": float(p.market_value),
                "unrealized_pl": float(p.unrealized_pl),
                "unrealized_plpc": float(p.unrealized_plpc),
            })
        
        return {"positions": pos_list, "deployed": deployed}
    except Exception as e:
        return {"positions": [], "deployed": 0, "error": str(e)}


def init_db():
    _ensure_tables()


init_db()


def get_last_bot_run_at():
    """Get the timestamp of the most recent bot run."""
    try:
        from dashboard.db import _ensure_tables, _con
        _ensure_tables()
        with _con() as c:
            row = c.execute(
                "SELECT COALESCE(finished_at, started_at) as last_at FROM bot_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return row["last_at"] if row else None
    except Exception:
        return None
