"""Live Alpaca paper trader V2 — supports all registered strategies.

Usage:
    python bot.py                               # trend_pullback
    python bot.py --strategy breakout
    python bot.py --strategy momentum_macd
    python bot.py --strategy ensemble
    python bot.py --loop                        # continuous loop
"""
from __future__ import annotations

import argparse
import math
import sys
import time
import uuid
from datetime import datetime, date, timezone, timedelta, time as dtime
from zoneinfo import ZoneInfo
from pathlib import Path

import pandas as pd
from alpaca.trading.requests import GetCalendarRequest

from config import PARAMS, TICKERS, ALPACA_KEY, ALPACA_SECRET, ALPACA_PAPER, StrategyType, BAR_TIMEFRAME
from logger_setup import get_logger
from strategies import REGISTRY, add_indicators, is_tp_reachable_in_days, split_qty
from dashboard import db as db_mod
from dashboard import bot_hooks
from notifier import send_notification
import data_feed
import runtime

log = get_logger(__name__)
ROOT = Path(__file__).parent
SERVICE = "bot"  # name used for run/bot.pid, run/bot.meta.json, run/bot.heartbeat

# Every order this bot places carries a client_order_id beginning with this prefix.
# It is our correlation id with Alpaca and the proof of ownership: the bot will only
# ever close a position whose originating order carries this prefix.
CLIENT_ORDER_PREFIX = "swingv2"


def _make_client_order_id(strategy: str, ticker: str, kind: str) -> str:
    """Unique, Alpaca-safe correlation id, e.g. swingv2-entry-ensemble-ARM-9f3a1c2b."""
    return f"{CLIENT_ORDER_PREFIX}-{kind}-{strategy}-{ticker}-{uuid.uuid4().hex[:8]}"


def stepped_stop_target(n_tp_filled: int, entry: float, initial_sl: float, tp1: float):
    """Where the stop should sit given how many TP legs have filled.

    0 -> initial SL, 1 -> entry (breakeven), 2 -> TP1, 3 -> None (position closed).
    """
    return [initial_sl, entry, tp1, None][min(int(n_tp_filled), 3)]


def _place_scaled_entry(tc, ticker: str, qty: int, sig, strat_name: str) -> dict:
    """Market buy + 3 limit-sell TP legs + a full-qty protective stop. Bot-owned."""
    from alpaca.trading.requests import (MarketOrderRequest, LimitOrderRequest, StopOrderRequest)
    from alpaca.trading.enums import OrderSide, TimeInForce

    entry_coid = _make_client_order_id(strat_name, ticker, "entry")
    buy = MarketOrderRequest(symbol=ticker, qty=qty, side=OrderSide.BUY,
                             time_in_force=TimeInForce.DAY, client_order_id=entry_coid)
    entry_order = tc.submit_order(buy)

    legs = split_qty(qty)            # [a, a, qty-2a]
    tps = [sig.tp1, sig.tp2, sig.tp3]
    leg_orders = []
    for i, (lq, tp) in enumerate(zip(legs, tps), start=1):
        coid = _make_client_order_id(strat_name, ticker, f"tp{i}")
        req = LimitOrderRequest(symbol=ticker, qty=lq, side=OrderSide.SELL,
                                time_in_force=TimeInForce.GTC,
                                limit_price=round(tp, 2), client_order_id=coid)
        leg_orders.append(tc.submit_order(req))

    stop_coid = _make_client_order_id(strat_name, ticker, "stop")
    stop_req = StopOrderRequest(symbol=ticker, qty=qty, side=OrderSide.SELL,
                                time_in_force=TimeInForce.GTC,
                                stop_price=round(sig.stop_loss, 2), client_order_id=stop_coid)
    stop_order = tc.submit_order(stop_req)

    return {"entry": entry_order, "tp_legs": leg_orders, "stop": stop_order,
            "entry_coid": entry_coid, "alpaca_id": str(getattr(entry_order, "id", "") or "")}


def _place_stop_only_entry(
    tc, ticker: str, qty: int, stop_price: float, strat_name: str
) -> dict:
    """Market entry with a broker-held stop and no take-profit leg."""
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest, StopLossRequest

    entry_coid = _make_client_order_id(strat_name, ticker, "entry")
    request = MarketOrderRequest(
        symbol=ticker,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.OTO,
        stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
        client_order_id=entry_coid,
    )
    order = tc.submit_order(request)
    return {
        "entry": order,
        "entry_coid": entry_coid,
        "alpaca_id": str(getattr(order, "id", "") or ""),
    }


def _position_qty(tc, ticker: str) -> float:
    try:
        pos = tc.get_open_position(ticker)
        return abs(float(pos.qty)) if pos else 0.0
    except Exception:
        return 0.0


def _status_str(obj) -> str:
    """Lowercase status value off an Alpaca order/leg.

    alpaca-py's OrderStatus is a (str, Enum): str(OrderStatus.FILLED) renders as
    "OrderStatus.FILLED", not "filled", because Enum.__str__ wins over the str
    mixin. Comparing that against plain-value strings never matches, so anything
    gating on order status must go through .value first.
    """
    status = getattr(obj, "status", "") or ""
    return str(getattr(status, "value", status)).lower()


def _our_sell_orders(tc, ticker: str):
    """Open + recently-closed SELL orders for the symbol that we own."""
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus, OrderSide
    out = []
    for st in (QueryOrderStatus.OPEN, QueryOrderStatus.CLOSED):
        try:
            req = GetOrdersRequest(status=st, symbols=[ticker], side=OrderSide.SELL, limit=50)
            out.extend(tc.get_orders(filter=req) or [])
        except Exception:
            pass
    return [o for o in out if str(getattr(o, "client_order_id", "") or "").startswith(CLIENT_ORDER_PREFIX)]


def _count_filled_tp_legs(tc, trade: dict) -> int:
    n = 0
    for o in _our_sell_orders(tc, trade["ticker"]):
        coid = str(getattr(o, "client_order_id", "") or "")
        if "-tp" in coid and _status_str(o) in ("filled", "done_for_day", "closed"):
            n += 1
    return n


def _open_stop_order(tc, trade: dict):
    for o in _our_sell_orders(tc, trade["ticker"]):
        coid = str(getattr(o, "client_order_id", "") or "")
        if "-stop-" in coid and _status_str(o) in ("new", "accepted", "held", "pending_new"):
            return o
    return None


def _sync_stepped_stop(tc, trade: dict):
    """Move the resting stop to match how many TP legs have filled (breakeven/TP1)."""
    from strategy import split_take_profit
    entry = float(trade["entry_price"])
    tp1, _, _ = split_take_profit(entry, float(trade["take_profit"]))

    n = _count_filled_tp_legs(tc, trade)
    target = stepped_stop_target(n, entry, float(trade["stop_loss"]), tp1)
    if target is None:
        return  # all TPs filled; reconciliation closes the trade elsewhere

    stop = _open_stop_order(tc, trade)
    if stop is None:
        return
    if abs(float(getattr(stop, "stop_price", 0.0)) - target) < 1e-6:
        return  # already at the right level

    qty = _position_qty(tc, trade["ticker"]) or float(getattr(stop, "qty", 0) or 0)
    if qty < 1:
        return
    try:
        tc.cancel_order_by_id(stop.id)
        from alpaca.trading.requests import StopOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        coid = _make_client_order_id(trade["strategy"], trade["ticker"], "stop")
        tc.submit_order(StopOrderRequest(symbol=trade["ticker"], qty=int(qty),
                                         side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
                                         stop_price=round(target, 2), client_order_id=coid))
        log.info("  %s stepped stop -> $%.2f (%d TP legs filled)", trade["ticker"], target, n)
    except Exception as e:
        log.error("  Failed to move stepped stop for %s: %s", trade["ticker"], e)


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

def fetch_bars(
    ticker: str, days: int = 90, timeframe: str = BAR_TIMEFRAME
) -> pd.DataFrame:
    """Recent bars for live trading, selected by strategy timeframe."""
    return data_feed.fetch_recent(ticker, days=days + 30, timeframe=timeframe)


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
    frames: dict[str, pd.DataFrame] = {}

    try:
        strat_obj = REGISTRY[strat_name]

        for ticker in TICKERS:
            log.info("Checking %s...", ticker)
            df = fetch_bars(
                ticker, days=PARAMS.history_days, timeframe=strat_obj.timeframe
            )
            df = data_feed.completed_bars(df, strat_obj.timeframe)
            if df.empty or len(df) < 60:
                log.warning("%s: insufficient data (got %d bars)", ticker, len(df))
                continue

            df = add_indicators(df, PARAMS)
            frames[ticker] = df
            idx = len(df) - 1

            # Check for entry signal
            sig = strat_obj.check_entry(df, idx, PARAMS)
            if sig is not None:
                trades_found += 1
                if strat_obj.has_take_profit:
                    log.info("  SIGNAL: %s entry at $%.2f (SL $%.2f / TP $%.2f)",
                             ticker, sig.entry_price, sig.stop_loss, sig.take_profit)
                else:
                    log.info("  SIGNAL: %s entry at $%.2f (cross exit / emergency stop)",
                             ticker, sig.entry_price)
                bot_hooks.log_signal(sig, ticker, strat_name)

                # Only enter if the nearest target (TP1) is reachable within ~2 trading days
                if (strat_obj.has_take_profit and
                        not is_tp_reachable_in_days(sig.entry_price, sig.tp1, sig.atr, days=4)):
                    log.info("  TP1 $%.2f not reachable (ATR=%.2f) — skipping",
                             sig.tp1, sig.atr)
                    continue

                # Skip if THIS bot already has an open trade for the ticker.
                if db_mod.get_open_trade(ticker, strat_name):
                    log.info("  Bot already holds an open %s trade — skipping", ticker)
                    continue

                # Also avoid stacking on top of any pre-existing position (e.g. one a
                # human opened): don't add exposure to a symbol we don't already manage.
                try:
                    tc = _get_trading()
                    open_pos = tc.get_open_position(ticker)
                    if open_pos:
                        log.info("  A %s position already exists (qty %s) not tracked by the bot — skipping",
                                 ticker, open_pos.qty)
                        continue
                except Exception:
                    pass  # No position — good to proceed

                # Place order
                try:
                    tc = _get_trading()
                    if strat_obj.exit_mode == "signal_with_stop":
                        snapshot = data_feed.fetch_snapshots([ticker]).get(ticker, {})
                        market_ref = float(snapshot.get("price") or 0)
                        if not math.isfinite(market_ref) or market_ref <= 0:
                            log.warning("  %s: no valid live price — skipping protected entry", ticker)
                            continue
                    else:
                        market_ref = sig.entry_price

                    qty = int(PARAMS.dollars_per_trade / market_ref)
                    if qty < 1:
                        # $200/trade can't buy a whole share of a >$200 stock.
                        # Skip entirely (and do NOT notify — this was the "Qty 0" spam).
                        log.info("  $%.0f/trade buys <1 whole share of %s @ $%.2f — skipping "
                                 "(no order, no notify)",
                                 PARAMS.dollars_per_trade, ticker, market_ref)
                        continue

                    if strat_obj.exit_mode == "signal_with_stop":
                        sig.stop_loss = market_ref * (1.0 - PARAMS.sma_cross_stop_loss_pct)
                        info = _place_stop_only_entry(
                            tc, ticker, qty, sig.stop_loss, strat_name
                        )
                        coid, alpaca_id = info["entry_coid"], info["alpaca_id"]
                        log.info("  Stop-only OTO: %s x%d (SL $%.2f) [coid=%s]",
                                 ticker, qty, sig.stop_loss, coid)
                    elif qty >= 3:
                        # Full 3-TP scale-out (market entry + 3 limit legs + managed stop)
                        info = _place_scaled_entry(tc, ticker, qty, sig, strat_name)
                        coid, alpaca_id = info["entry_coid"], info["alpaca_id"]
                        log.info("  Scaled entry: %s x%d (TP1 $%.2f / TP2 $%.2f / TP3 $%.2f, "
                                 "SL $%.2f) [coid=%s]", ticker, qty, sig.tp1, sig.tp2, sig.tp3,
                                 sig.stop_loss, coid)
                    else:
                        # qty 1-2: too few shares to scale out — single OCO bracket at TP3
                        from alpaca.trading.requests import (MarketOrderRequest,
                                                             TakeProfitRequest, StopLossRequest)
                        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
                        tp = TakeProfitRequest(limit_price=round(sig.tp3, 2))
                        sl = StopLossRequest(stop_price=round(sig.stop_loss, 2))
                        coid = _make_client_order_id(strat_name, ticker, "entry")
                        mkt = MarketOrderRequest(symbol=ticker, qty=qty, side=OrderSide.BUY,
                                                 time_in_force=TimeInForce.DAY,
                                                 order_class=OrderClass.BRACKET,
                                                 take_profit=tp, stop_loss=sl,
                                                 client_order_id=coid)
                        order = tc.submit_order(mkt)
                        alpaca_id = str(getattr(order, "id", "") or "")
                        log.info("  Single bracket (qty<3): %s x%d @ $%.2f (SL $%.2f / TP $%.2f) [coid=%s]",
                                 ticker, qty, sig.entry_price, sig.stop_loss, sig.tp3, coid)

                    orders_placed += 1
                    db_mod.save_trade(ticker, strat_name, str(sig.date),
                                      sig.entry_price, sig.stop_loss, sig.tp3,
                                      shares=qty, client_order_id=coid,
                                      alpaca_order_id=alpaca_id)
                    if strat_obj.has_take_profit:
                        body = (f"Entry ${sig.entry_price:.2f}\nSL ${sig.stop_loss:.2f}\n"
                                f"TP1 ${sig.tp1:.2f} / TP2 ${sig.tp2:.2f} / TP3 ${sig.tp3:.2f}\n"
                                f"Qty {qty}\nRef {coid}")
                    else:
                        body = (f"Daily SMA(50) cross entry ~${market_ref:.2f}\n"
                                f"SL ${sig.stop_loss:.2f}\nQty {qty}\nRef {coid}")
                    send_notification(f"Bot V2: {ticker} entry ({strat_name})", body)

                except Exception as e:
                    log.error("  Order failed for %s: %s", ticker, e)
            else:
                log.info("  No signal for %s", ticker)

        # Reconcile + apply exits — only on positions THIS bot opened
        _reconcile_and_exit(strat_name, frames)

    except Exception as e:
        error = str(e)
        log.error("Bot run failed: %s", e)
        send_notification("Bot V2 Error", f"{e}\n\nRun: {datetime.now().isoformat()}")

    run_status = "error" if error else "done"
    db_mod.finish_bot_run(run_id, trades_found, orders_placed, error)
    log.info("Bot V2 run complete: %d signals, %d orders — status=%s", trades_found, orders_placed, run_status)
    return 0 if not error else 1


def _max_hold_days(strategy: str) -> int:
    """Per-strategy time-stop horizon (calendar days)."""
    return {
        "trend_pullback": PARAMS.max_holding_days,
        "breakout": PARAMS.breakout_max_holding_days,
        "mean_reversion": PARAMS.mr_max_holding_days,
        "momentum_macd": PARAMS.macd_max_holding_days,
        "ensemble": PARAMS.ensemble_max_holding_days,
        "regime": PARAMS.max_holding_days,
    }.get(strategy, PARAMS.max_holding_days)


def _days_held(entry_date: str) -> int:
    try:
        return (date.today() - pd.to_datetime(entry_date).date()).days
    except Exception:
        return 0


def _verify_owned(tc, trade: dict) -> bool:
    """Prove this trade is ours: the Alpaca entry order must carry our correlation id.

    Fails CLOSED — if we cannot positively confirm ownership we return False, so the
    caller leaves the position untouched. We never close what we cannot prove we own.
    """
    coid = trade.get("client_order_id")
    if not coid or not str(coid).startswith(CLIENT_ORDER_PREFIX):
        return False
    try:
        order = tc.get_order_by_client_id(coid)
        return order is not None and getattr(order, "symbol", None) == trade["ticker"]
    except Exception:
        return False


def _reconcile_and_exit(
    strat_name: str, frames: dict[str, pd.DataFrame] | None = None
):
    """Keep DB trades in sync and apply the selected strategy's live exit.

    For each open trade THIS bot recorded:
      * if its Alpaca position is gone (a bracket SL/TP filled) → record the exit.
      * else if past max-hold and at breakeven+ → close OUR quantity (after verifying
        ownership and cancelling our own bracket legs).
    Positions the bot did not open are never inspected or closed.
    """
    try:
        tc = _get_trading()
    except Exception as e:
        log.debug("Exit check skipped (no trading client): %s", e)
        return

    strat_obj = REGISTRY[strat_name]
    for trade in db_mod.get_open_trades_by_strategy(strat_name):
        ticker = trade["ticker"]
        try:
            try:
                pos = tc.get_open_position(ticker)
            except Exception:
                pos = None  # Alpaca raises when there is no position for the symbol

            if pos is None:
                _reconcile_closed(tc, trade)
                continue

            if strat_obj.exit_mode == "signal_with_stop":
                frame = (frames or {}).get(ticker)
                if frame is None or frame.empty:
                    log.warning("  %s: no completed daily frame — leaving position open", ticker)
                    continue
                if not _verify_owned(tc, trade):
                    log.warning("  %s cross exit blocked: ownership unverified (coid=%s)",
                                ticker, trade.get("client_order_id"))
                    continue
                reason = strat_obj.check_exit(frame, len(frame) - 1, PARAMS)
                if reason:
                    _close_owned(tc, trade, pos, reason=reason)
                continue

            # Ratchet the stepped stop (breakeven after TP1, TP1 after TP2) for our
            # scaled positions. No-op for single-bracket trades (no managed stop order).
            if _verify_owned(tc, trade):
                _sync_stepped_stop(tc, trade)

            held = _days_held(trade["entry_date"])
            # max-hold is expressed in 4h bars (~2 per trading day); convert to a
            # calendar-day backstop for the live time-stop so it tracks the backtest.
            max_hold = max(1, round(_max_hold_days(strat_name) / 2))
            current = float(pos.current_price)
            entry = float(trade["entry_price"])
            if held >= max_hold and current >= entry:
                if not _verify_owned(tc, trade):
                    log.warning("  %s past max-hold but ownership unverified (coid=%s) — leaving it alone",
                                ticker, trade.get("client_order_id"))
                    continue
                _close_owned(tc, trade, pos, reason="time_stop")
        except Exception as e:
            log.error("  Exit check failed for %s: %s", ticker, e)


def _reconcile_closed(tc, trade: dict):
    """Our tracked position is gone — either a bracket leg filled, or the entry
    order itself never filled and was later canceled/expired by the broker."""
    ticker = trade["ticker"]
    entry = float(trade["entry_price"])
    shares = float(trade.get("shares") or 0)

    if _entry_never_filled(tc, trade):
        db_mod.close_trade(trade["id"], datetime.now(timezone.utc).isoformat(), entry,
                           "entry_not_filled", _days_held(trade["entry_date"]), 0.0, 0.0, 0.0,
                           exit_client_order_id=trade.get("client_order_id"),
                           exit_alpaca_order_id=trade.get("alpaca_order_id"))
        log.info("  Reconciled: %s entry order never filled for trade %s (coid=%s) — closing, no position taken",
                 ticker, trade.get("id"), trade.get("client_order_id"))
        return True

    exit_fill = _confirmed_exit_fill(tc, trade)
    if exit_fill is None:
        log.warning("  %s position missing but no confirmed exit fill for trade %s (coid=%s) — leaving open",
                    ticker, trade.get("id"), trade.get("client_order_id"))
        return False

    exit_price = exit_fill["price"]
    exit_coid = exit_fill["client_order_id"]
    exit_id = exit_fill["alpaca_order_id"]
    if exit_fill.get("shares"):
        shares = exit_fill["shares"] or shares
    strat_obj = REGISTRY.get(trade.get("strategy", ""))
    reason = (
        "stop_loss"
        if strat_obj is not None and strat_obj.exit_mode == "signal_with_stop"
        else "bracket_filled"
    )

    pnl = (exit_price - entry) * shares
    pnl_pct = (exit_price - entry) / entry if entry else 0.0
    db_mod.close_trade(trade["id"], datetime.now(timezone.utc).isoformat(), exit_price,
                       reason, _days_held(trade["entry_date"]), shares, pnl, pnl_pct,
                       exit_client_order_id=exit_coid, exit_alpaca_order_id=exit_id)
    log.info("  Reconciled exit: %s closed @ $%.2f (%s) pnl=$%.2f", ticker, exit_price, reason, pnl)
    return True


def _entry_never_filled(tc, trade: dict) -> bool:
    """True if the order meant to open this trade was canceled/expired/rejected
    without ever filling any shares — i.e. no position was ever taken."""
    for order in _entry_order_candidates(tc, trade):
        filled_qty = float(getattr(order, "filled_qty", 0) or 0)
        if _status_str(order) in ("canceled", "expired", "rejected") and filled_qty == 0:
            return True
    return False


def _confirmed_exit_fill(tc, trade: dict) -> dict | None:
    """Return a filled sell tied to this DB trade, never an arbitrary old sell.

    For single-share bracket orders, Alpaca keeps the stop/TP children under the
    parent entry order's legs. Scaled entries use independent bot-owned sell
    orders, so those must carry our client-order prefix and belong to this
    strategy/ticker after the DB trade was created. Either way, a fill already
    claimed as another trade's exit is skipped — one broker fill can only close
    one DB trade.
    """
    for order in _entry_order_candidates(tc, trade):
        for leg in (getattr(order, "legs", None) or []):
            fill = _exit_fill_from_order(leg, require_prefix=False)
            if fill and not db_mod.exit_order_already_used(fill["alpaca_order_id"]):
                return fill

    marker = f"-{trade['strategy']}-{trade['ticker']}-"
    for order in _our_sell_orders(tc, trade["ticker"]):
        coid = str(getattr(order, "client_order_id", "") or "")
        if marker not in coid:
            continue
        if not _order_is_after_trade(order, trade):
            continue
        fill = _exit_fill_from_order(order, require_prefix=True)
        if fill and not db_mod.exit_order_already_used(fill["alpaca_order_id"]):
            return fill

    return None


def _entry_order_candidates(tc, trade: dict) -> list:
    from alpaca.trading.requests import GetOrderByIdRequest
    out = []
    entry_id = trade.get("alpaca_order_id")
    if entry_id:
        try:
            # nested=True is required or Alpaca omits bracket child legs entirely.
            out.append(tc.get_order_by_id(entry_id, filter=GetOrderByIdRequest(nested=True)))
        except Exception:
            pass

    coid = trade.get("client_order_id")
    if coid:
        try:
            out.append(tc.get_order_by_client_id(coid))
        except Exception:
            pass
    return [o for o in out if o is not None]


def _exit_fill_from_order(order, require_prefix: bool) -> dict | None:
    coid = str(getattr(order, "client_order_id", "") or "")
    if require_prefix and not coid.startswith(CLIENT_ORDER_PREFIX):
        return None
    if _status_str(order) not in ("filled", "closed"):
        return None
    if getattr(order, "filled_avg_price", None) is None:
        return None
    return {
        "price": float(order.filled_avg_price),
        "client_order_id": getattr(order, "client_order_id", None),
        "alpaca_order_id": str(getattr(order, "id", "") or ""),
        "shares": float(getattr(order, "filled_qty", 0) or 0),
    }


def _order_is_after_trade(order, trade: dict) -> bool:
    trade_ts = _parse_timestamp(trade.get("created_at")) or _parse_timestamp(trade.get("entry_date"))
    order_ts = (
        _parse_timestamp(getattr(order, "filled_at", None))
        or _parse_timestamp(getattr(order, "updated_at", None))
        or _parse_timestamp(getattr(order, "submitted_at", None))
    )
    if trade_ts is None or order_ts is None:
        return False
    return order_ts >= trade_ts - timedelta(minutes=5)


def _parse_timestamp(value):
    if not value:
        return None
    try:
        ts = pd.to_datetime(value, utc=True)
        return ts.to_pydatetime()
    except Exception:
        return None


def _close_owned(tc, trade: dict, pos, reason: str):
    """Close ONLY the quantity this bot opened, after cancelling our own bracket legs."""
    ticker = trade["ticker"]
    entry = float(trade["entry_price"])
    our_qty = float(trade.get("shares") or 0)
    pos_qty = abs(float(pos.qty))
    qty_to_close = int(min(our_qty, pos_qty)) if our_qty > 0 else int(pos_qty)
    if qty_to_close < 1:
        log.warning("  %s: nothing to close (our_qty=%s pos_qty=%s)", ticker, our_qty, pos_qty)
        return

    # Free the shares by cancelling the still-open SL/TP legs of OUR entry order only.
    try:
        from alpaca.trading.requests import GetOrderByIdRequest
        entry_id = trade.get("alpaca_order_id")
        if entry_id:
            entry_order = tc.get_order_by_id(entry_id, filter=GetOrderByIdRequest(nested=True))
            for leg in (getattr(entry_order, "legs", None) or []):
                if _status_str(leg) in (
                        "new", "accepted", "held", "pending_new", "partially_filled"):
                    try:
                        tc.cancel_order_by_id(leg.id)
                        log.info("  Cancelled bracket leg %s for %s", leg.id, ticker)
                    except Exception as e:
                        log.debug("  Could not cancel leg %s: %s", getattr(leg, "id", "?"), e)
    except Exception as e:
        log.debug("  Could not inspect bracket legs for %s: %s", ticker, e)

    exit_coid = _make_client_order_id(trade["strategy"], ticker, "exit")
    exit_id = ""
    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        sell = MarketOrderRequest(symbol=ticker, qty=qty_to_close, side=OrderSide.SELL,
                                  time_in_force=TimeInForce.DAY, client_order_id=exit_coid)
        order = tc.submit_order(sell)
        exit_id = str(getattr(order, "id", "") or "")
        log.info("  Closed bot position %s x%d (%s) [coid=%s]", ticker, qty_to_close, reason, exit_coid)
    except Exception as e:
        log.error("  Failed to close %s: %s", ticker, e)
        return

    current = float(pos.current_price)
    pnl = (current - entry) * qty_to_close
    pnl_pct = (current - entry) / entry if entry else 0.0
    db_mod.close_trade(trade["id"], datetime.now(timezone.utc).isoformat(), current,
                       reason, _days_held(trade["entry_date"]), qty_to_close, pnl, pnl_pct,
                       exit_client_order_id=exit_coid, exit_alpaca_order_id=exit_id)
    send_notification(
        f"Bot V2: {ticker} exit ({trade['strategy']})",
        f"Closed x{qty_to_close} @ ~${current:.2f}\nReason {reason}\nPnL ${pnl:.2f}\nRef {exit_coid}"
    )


_ET = ZoneInfo("America/New_York")
_MARKET_OPEN  = dtime(8, 30)
_MARKET_CLOSE = dtime(17, 0)


def _in_trading_hours() -> bool:
    now_et = datetime.now(_ET)
    now_time = now_et.time().replace(second=0, microsecond=0)
    if now_et.weekday() >= 5:
        return False
    if not (_MARKET_OPEN <= now_time < _MARKET_CLOSE):
        return False
    try:
        return bool(_get_trading().get_calendar(GetCalendarRequest(
            start=now_et.date(),
            end=now_et.date(),
        )))
    except Exception as e:
        log.debug("Market calendar unavailable; using weekday/time fallback: %s", e)
        return True


def run_loop(strategy: StrategyType, interval_minutes: int = 30):
    """Run bot in continuous loop, active only 08:30–17:00 ET."""
    log.info("Starting Bot V2 loop — %s every %d min", strategy.value, interval_minutes)
    while True:
        runtime.heartbeat(SERVICE)
        if _in_trading_hours():
            run_once(strategy)
            runtime.heartbeat(SERVICE)
        else:
            now_et = datetime.now(_ET)
            log.info("Outside trading hours (%s ET) — skipping run", now_et.strftime("%H:%M"))
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

    # Record our PID + run metadata so manage.ps1 can detect a live, healthy
    # instance and refuse to spawn a duplicate (the cause of the email flood).
    runtime.register(SERVICE, {
        "strategy": strategy.value,
        "interval": args.interval,
        "loop": bool(args.loop),
        "cmd": "python " + " ".join(sys.argv),
    })

    if args.loop:
        run_loop(strategy, args.interval)
    else:
        sys.exit(run_once(strategy))


if __name__ == "__main__":
    main()
