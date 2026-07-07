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
        "exit_client_order_id": "TEXT",  # correlation id of the closing order
        "exit_alpaca_order_id": "TEXT",  # Alpaca's order UUID for the exit
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
               alpaca_order_id: Optional[str] = None) -> int:
    """Persist a newly opened trade, including its Alpaca correlation ids."""
    _ensure_tables()
    with _con() as c:
        cur = c.execute(
            "INSERT INTO trades (ticker, strategy, entry_date, entry_price, stop_loss, "
            "take_profit, shares, client_order_id, alpaca_order_id, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,'open')",
            (ticker, strategy, entry_date, entry_price, stop_loss, take_profit,
             shares, client_order_id, alpaca_order_id),
        )
        return cur.lastrowid


def close_trade(db_id: int, exit_date: str, exit_price: float, reason: str,
                bars_held: int, shares: float, pnl_dollars: float, pnl_pct: float,
                exit_client_order_id: Optional[str] = None,
                exit_alpaca_order_id: Optional[str] = None):
    with _con() as c:
        c.execute(
            "UPDATE trades SET exit_date=?, exit_price=?, exit_reason=?, bars_held=?, "
            "shares=?, pnl_dollars=?, pnl_pct=?, exit_client_order_id=?, "
            "exit_alpaca_order_id=?, status='closed' WHERE id=?",
            (exit_date, exit_price, reason, bars_held, shares, pnl_dollars, pnl_pct,
             exit_client_order_id, exit_alpaca_order_id, db_id),
        )


def exit_order_already_used(exit_alpaca_order_id: Optional[str]) -> bool:
    """True if some other trade already claims this Alpaca order as its exit fill.

    Guards against reconciliation attributing one broker fill to several DB rows.
    """
    if not exit_alpaca_order_id:
        return False
    with _con() as c:
        row = c.execute(
            "SELECT 1 FROM trades WHERE exit_alpaca_order_id=? LIMIT 1",
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
        closed = c.execute("""
            SELECT COUNT(*) as trades,
                   SUM(CASE WHEN pnl_dollars > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN pnl_dollars <= 0 THEN 1 ELSE 0 END) as losses,
                   COALESCE(SUM(pnl_dollars), 0) as total_pnl,
                   COALESCE(AVG(pnl_pct), 0) as avg_pnl_pct,
                   COALESCE(SUM(CASE WHEN pnl_dollars > 0 THEN pnl_dollars ELSE 0 END), 0) as gross_profit,
                   COALESCE(SUM(CASE WHEN pnl_dollars < 0 THEN ABS(pnl_dollars) ELSE 0 END), 0) as gross_loss
            FROM trades WHERE status='closed'
        """).fetchone()

        open_count = c.execute("SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()[0]

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