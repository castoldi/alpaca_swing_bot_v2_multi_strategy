"""Bridge between bot.py and database — same pattern as V1."""
from __future__ import annotations

from dashboard import db as db_mod
from strategy import EntrySignal


def log_signal(signal: EntrySignal, ticker: str, strategy: str) -> int:
    """Record an entry signal in the database."""
    return db_mod.save_signal(
        ticker=ticker,
        strategy=strategy,
        signal_date=str(signal.date),
        entry_price=signal.entry_price,
        stop_loss=signal.stop_loss,
        take_profit=signal.take_profit,
        atr=signal.atr,
        rsi=signal.rsi,
    )


def sync_positions_from_alpaca(trading_client):
    """Sync live Alpaca positions to DB."""
    return db_mod.sync_positions_from_alpaca(trading_client)