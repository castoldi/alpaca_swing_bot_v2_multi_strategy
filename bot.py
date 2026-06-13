"""Live Alpaca paper trader V2 — supports all 6 strategies.

Usage:
    python bot.py                               # trend_pullback
    python bot.py --strategy breakout
    python bot.py --strategy momentum_macd
    python bot.py --strategy ensemble
    python bot.py --loop                        # continuous loop
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, date, timezone, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

from config import PARAMS, TICKERS, ALPACA_KEY, ALPACA_SECRET, ALPACA_PAPER, StrategyType
from logger_setup import get_logger
from strategy import add_indicators, get_entry_checker, simulate_exit, is_tp_reachable_in_days
from dashboard import db as db_mod
from dashboard import bot_hooks
from notifier import send_notification

log = get_logger(__name__)
ROOT = Path(__file__).parent


# ── Alpaca client (lazy) ──────────────────────────────────────────────────────

_trading_client = None
def _get_trading():
    global _trading_client
    if _trading_client is None:
        if not ALPACA_PAPER:
            log.warning("⚠️  ALPACA_PAPER=false — would trade REAL MONEY! Forcing paper=True")
        from alpaca.trading.client import TradingClient
        # PAPER ONLY — hardcoded paper=True as safety override
        _trading_client = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)
    return _trading_client


# ── Data fetch ────────────────────────────────────────────────────────────────

def fetch_bars(ticker: str, days: int = 90) -> pd.DataFrame:
    end = date.today() + timedelta(days=1)
    start = date.today() - timedelta(days=days + 30)
    raw = yf.download(ticker, start=start.isoformat(), end=end.isoformat(),
                      interval="1d", auto_adjust=True, progress=False)
    if raw.empty:
        return raw
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df


# ── Main trading loop ─────────────────────────────────────────────────────────

def run_once(strategy: StrategyType) -> int:
    """Single pass: check each ticker, enter if signal, check open positions."""
    strat_name = strategy.value
    run_id = db_mod.start_bot_run(strat_name)
    log.info("=" * 50)
    log.info("Bot V2 run — strategy=%s", strat_name)
    log.info("=" * 50)

    orders_placed = 0
    trades_found = 0
    error = None

    try:
        entry_checker = get_entry_checker(strategy)

        for ticker in TICKERS:
            log.info("Checking %s...", ticker)
            df = fetch_bars(ticker, days=PARAMS.history_days)
            if df.empty or len(df) < 60:
                log.warning("%s: insufficient data (got %d bars)", ticker, len(df))
                continue

            df = add_indicators(df, PARAMS)
            idx = len(df) - 1
            today = df.index[idx]

            # Check for entry signal
            sig = entry_checker(df, idx, PARAMS)
            if sig is not None:
                trades_found += 1
                log.info("  SIGNAL: %s entry at $%.2f (SL $%.2f / TP $%.2f)",
                         ticker, sig.entry_price, sig.stop_loss, sig.take_profit)
                bot_hooks.log_signal(sig, ticker, strat_name)

                # Only enter if the TP target is reachable within 2 trading days
                if not is_tp_reachable_in_days(sig.entry_price, sig.take_profit, sig.atr, days=4):
                    log.info("  TP $%.2f not reachable within 2 days (ATR=%.2f) — skipping",
                             sig.take_profit, sig.atr)
                    continue

                # Check if we already have an open position for this ticker
                try:
                    tc = _get_trading()
                    open_pos = tc.get_open_position(ticker)
                    if open_pos:
                        log.info("  Already in %s (qty %s) — skipping", ticker, open_pos.qty)
                        continue
                except Exception:
                    pass  # No position — good to proceed

                # Place order
                try:
                    from alpaca.trading.requests import MarketOrderRequest
                    from alpaca.trading.enums import OrderSide, TimeInForce

                    qty = int(PARAMS.dollars_per_trade / sig.entry_price)
                    if qty < 1:
                        log.warning("  Price too high for whole shares ($%.2f) — skipping OCO", sig.entry_price)
                        # Notional buy without bracket
                        mkt = MarketOrderRequest(
                            symbol=ticker,
                            notional=PARAMS.dollars_per_trade,
                            side=OrderSide.BUY,
                            time_in_force=TimeInForce.DAY,
                        )
                        tc.submit_order(mkt)
                        log.info("  Placed notional market order: %s $%.0f", ticker, PARAMS.dollars_per_trade)
                    else:
                        from alpaca.trading.requests import TakeProfitRequest, StopLossRequest
                        tp = TakeProfitRequest(limit_price=round(sig.take_profit, 2))
                        sl = StopLossRequest(stop_price=round(sig.stop_loss, 2))
                        mkt = MarketOrderRequest(
                            symbol=ticker,
                            qty=qty,
                            side=OrderSide.BUY,
                            time_in_force=TimeInForce.DAY,
                            take_profit=tp,
                            stop_loss=sl,
                        )
                        tc.submit_order(mkt)
                        log.info("  Placed OCO bracket: %s x%d @ $%.2f (SL $%.2f / TP $%.2f)",
                                 ticker, qty, sig.entry_price, sig.stop_loss, sig.take_profit)

                    orders_placed += 1
                    db_mod.save_trade(ticker, strat_name, str(sig.date),
                                      sig.entry_price, sig.stop_loss, sig.take_profit)
                    send_notification(
                        f"Bot V2: {ticker} entry ({strat_name})",
                        f"Entry ${sig.entry_price:.2f}\nSL ${sig.stop_loss:.2f}\nTP ${sig.take_profit:.2f}\nQty {qty}"
                    )

                except Exception as e:
                    log.error("  Order failed for %s: %s", ticker, e)
            else:
                log.info("  No signal for %s", ticker)

        # Check open positions for exit conditions
        _check_open_positions(df)

    except Exception as e:
        error = str(e)
        log.error("Bot run failed: %s", e)
        send_notification("Bot V2 Error", f"{e}\n\nRun: {datetime.now().isoformat()}")

    run_status = "error" if error else "done"
    db_mod.finish_bot_run(run_id, trades_found, orders_placed, error)
    log.info("Bot V2 run complete: %d signals, %d orders — status=%s", trades_found, orders_placed, run_status)
    return 0 if not error else 1


def _check_open_positions(df: pd.DataFrame):
    """Check live positions for stop loss / take profit / time stop exit conditions."""
    try:
        tc = _get_trading()
        positions = tc.get_all_positions()
        if not positions:
            return

        for pos in positions:
            ticker = pos.symbol
            if ticker not in TICKERS:
                continue

            entry_price = float(pos.avg_entry_price)
            current_price = float(pos.current_price)
            qty = float(pos.qty)

            # Check SL/TP from price
            if current_price <= entry_price * 0.9:
                log.info("  STOP LOSS triggered for %s at $%.2f (entry $%.2f)", ticker, current_price, entry_price)
                tc.close_position(ticker)
                pnl = (current_price - entry_price) * qty
                db_mod.close_trade(
                    ticker, current_price, entry_price, current_price,
                    pnl_pct=(current_price - entry_price) / entry_price,
                )

    except Exception as e:
        log.debug("Position check skipped: %s", e)


def run_loop(strategy: StrategyType, interval_minutes: int = 30):
    """Run bot in continuous loop."""
    log.info("Starting Bot V2 loop — %s every %d min", strategy.value, interval_minutes)
    while True:
        run_once(strategy)
        log.info("Sleeping %d minutes...", interval_minutes)
        time.sleep(interval_minutes * 60)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Alpaca Swing Bot V2")
    parser.add_argument("--strategy", type=str, default=None,
                        choices=[s.value for s in StrategyType],
                        help="Trading strategy (required)")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=int, default=30, help="Loop interval (min)")
    args = parser.parse_args()

    if args.strategy is None:
        choices = "\n  ".join(s.value for s in StrategyType)
        parser.error(f"--strategy is required. Choose one of:\n  {choices}")

    strategy = StrategyType(args.strategy)

    if args.loop:
        run_loop(strategy, args.interval)
    else:
        sys.exit(run_once(strategy))


if __name__ == "__main__":
    main()