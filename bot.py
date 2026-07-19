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
from dataclasses import dataclass
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
from position_sizing import whole_share_position_size

log = get_logger(__name__)
ROOT = Path(__file__).parent
SERVICE = "bot"  # name used for run/bot.pid, run/bot.meta.json, run/bot.heartbeat

# Every order this bot places carries a client_order_id beginning with this prefix.
# It is our correlation id with Alpaca and the proof of ownership: the bot will only
# ever close a position whose originating order carries this prefix.
CLIENT_ORDER_PREFIX = "swingv2"
ENTRY_PENDING_GRACE = timedelta(minutes=5)


@dataclass
class LiveSizingState:
    equity: float
    remaining_cash: float
    remaining_slots: int


def _load_live_sizing(tc) -> LiveSizingState | None:
    """Read one safe, non-margin sizing snapshot for the current bot cycle."""
    try:
        account = tc.get_account()
        equity = float(account.equity)
        cash = float(account.cash)
        get_positions = getattr(tc, "get_all_positions", None)
        positions = get_positions() if callable(get_positions) else []
        open_position_count = len(positions or [])
        if (
            not math.isfinite(equity)
            or not math.isfinite(cash)
            or equity <= 0
            or cash < 0
            or open_position_count < 0
        ):
            raise ValueError("invalid equity or cash")
        return LiveSizingState(
            equity=equity,
            remaining_cash=cash,
            remaining_slots=max(
                0, PARAMS.max_concurrent_positions - open_position_count
            ),
        )
    except Exception as exc:
        log.warning("New entries disabled this cycle: account sizing unavailable (%s)", exc)
        return None


def _make_client_order_id(strategy: str, ticker: str, kind: str) -> str:
    """Unique, Alpaca-safe correlation id, e.g. swingv2-entry-ensemble-ARM-9f3a1c2b."""
    return f"{CLIENT_ORDER_PREFIX}-{kind}-{strategy}-{ticker}-{uuid.uuid4().hex[:8]}"


def stepped_stop_target(n_tp_filled: int, entry: float, initial_sl: float, tp1: float):
    """Where the stop should sit given how many TP legs have filled.

    0 -> initial SL, 1 -> entry (breakeven), 2 -> TP1, 3 -> None (position closed).
    """
    return [initial_sl, entry, tp1, None][min(int(n_tp_filled), 3)]


def _cancel_orders_confirmed(
    tc, orders: list, *, attempts: int = 5, delay: float = 0.2
) -> bool:
    """Request cancellation and confirm every order reaches a terminal state."""
    if not orders:
        return True

    order_ids = [str(getattr(order, "id", "") or "") for order in orders]
    if any(not order_id for order_id in order_ids):
        return False

    for order_id in order_ids:
        try:
            tc.cancel_order_by_id(order_id)
        except Exception as exc:
            log.error("  Could not request cancellation of TP leg %s: %s", order_id, exc)

    terminal = {"canceled", "expired", "rejected", "filled", "closed"}
    for attempt in range(attempts):
        try:
            statuses = {
                order_id: _status_str(tc.get_order_by_id(order_id))
                for order_id in order_ids
            }
        except Exception as exc:
            log.error("  Could not confirm TP cleanup: %s", exc)
            statuses = {}

        if statuses and all(
            statuses.get(order_id) in terminal for order_id in order_ids
        ):
            return True
        if attempt < attempts - 1:
            time.sleep(delay)

    return False


def _place_scaled_entry(
    tc,
    ticker: str,
    qty: int,
    sig,
    strat_name: str,
    entry_coid: str | None = None,
    on_entry_accepted=None,
) -> dict:
    """Atomic stop-protected market entry followed by three TP sell orders."""
    from alpaca.trading.requests import (
        LimitOrderRequest,
        MarketOrderRequest,
        StopLossRequest,
    )
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce

    entry_coid = entry_coid or _make_client_order_id(
        strat_name, ticker, "entry"
    )
    buy = MarketOrderRequest(
        symbol=ticker,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.OTO,
        stop_loss=StopLossRequest(stop_price=round(sig.stop_loss, 2)),
        client_order_id=entry_coid,
    )
    entry_order = tc.submit_order(buy)
    result = {
        "entry": entry_order,
        "tp_legs": [],
        "stop": None,
        "entry_coid": entry_coid,
        "alpaca_id": str(getattr(entry_order, "id", "") or ""),
        "setup_error": None,
        "cleanup_confirmed": True,
    }

    if on_entry_accepted is not None:
        try:
            on_entry_accepted(result)
        except Exception:
            try:
                tc.cancel_order_by_id(entry_order.id)
            except Exception as cancel_exc:
                log.error(
                    "  Could not cancel unrecorded protected entry %s: %s",
                    ticker,
                    cancel_exc,
                )
            raise

    legs = split_qty(qty)            # [a, a, qty-2a]
    tps = [sig.tp1, sig.tp2, sig.tp3]
    leg_orders = []
    try:
        for i, (lq, tp) in enumerate(zip(legs, tps), start=1):
            coid = _make_client_order_id(strat_name, ticker, f"tp{i}")
            req = LimitOrderRequest(
                symbol=ticker,
                qty=lq,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                limit_price=round(tp, 2),
                client_order_id=coid,
            )
            leg_orders.append(tc.submit_order(req))
    except Exception as exc:
        cleanup_confirmed = _cancel_orders_confirmed(tc, leg_orders)
        result["tp_legs"] = leg_orders
        result["setup_error"] = str(exc)
        result["cleanup_confirmed"] = cleanup_confirmed
        return result

    result["tp_legs"] = leg_orders
    return result


def _place_stop_only_entry(
    tc,
    ticker: str,
    qty: int,
    stop_price: float,
    strat_name: str,
    entry_coid: str | None = None,
) -> dict:
    """Market entry with a broker-held stop and no take-profit leg."""
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest, StopLossRequest

    entry_coid = entry_coid or _make_client_order_id(
        strat_name, ticker, "entry"
    )
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


def _place_single_bracket_entry(
    tc,
    ticker: str,
    qty: int,
    sig,
    strat_name: str,
    entry_coid: str | None = None,
) -> dict:
    """Submit one protected bracket when quantity is too small to scale out."""
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
    from alpaca.trading.requests import (
        MarketOrderRequest,
        StopLossRequest,
        TakeProfitRequest,
    )

    coid = entry_coid or _make_client_order_id(strat_name, ticker, "entry")
    request = MarketOrderRequest(
        symbol=ticker,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=round(sig.tp3, 2)),
        stop_loss=StopLossRequest(stop_price=round(sig.stop_loss, 2)),
        client_order_id=coid,
    )
    order = tc.submit_order(request)
    return {
        "entry": order,
        "entry_coid": coid,
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


def _exception_status_code(exc: Exception) -> int | None:
    value = getattr(exc, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _is_definite_submission_rejection(exc: Exception) -> bool:
    """True only for broker responses that definitively reject the request."""
    return _exception_status_code(exc) in {400, 401, 403, 404, 422}


def _lookup_entry_by_client_id(
    tc, client_order_id: str, *, attempts: int = 3, delay: float = 0.1
) -> tuple[str, object | None]:
    """Resolve an ambiguous submit as found, explicitly absent, or unknown."""
    only_not_found = True
    for attempt in range(attempts):
        try:
            order = tc.get_order_by_client_id(client_order_id)
            if order is not None:
                return "found", order
        except Exception as exc:
            if _exception_status_code(exc) != 404:
                only_not_found = False
        if attempt < attempts - 1:
            time.sleep(delay)
    return ("not_found" if only_not_found else "unknown"), None


def _resolve_pending_entry(tc, trade: dict) -> bool:
    """Adopt or retire a durable pre-submit intent during reconciliation."""
    if trade.get("entry_state") != "pending_submission":
        return True

    coid = str(trade.get("client_order_id") or "")
    if not coid:
        log.error("  Pending entry %s has no client id; leaving open", trade.get("id"))
        return False

    resolution, order = _lookup_entry_by_client_id(tc, coid)
    if resolution == "found":
        order_id = str(getattr(order, "id", "") or "") or None
        db_mod.set_entry_order_id(trade["id"], order_id)
        trade["alpaca_order_id"] = order_id
        trade["entry_state"] = "accepted"
        log.warning(
            "  Adopted pending %s entry by client id %s",
            trade["ticker"],
            coid,
        )
        return True

    created_at = _parse_timestamp(trade.get("created_at"))
    age = (
        datetime.now(timezone.utc) - created_at
        if created_at is not None
        else timedelta(0)
    )
    if resolution == "not_found" and age >= ENTRY_PENDING_GRACE:
        entry = float(trade.get("entry_price") or 0.0)
        db_mod.close_trade(
            trade["id"],
            datetime.now(timezone.utc).isoformat(),
            entry,
            "entry_not_submitted",
            0,
            0.0,
            0.0,
            0.0,
            exit_client_order_id=coid,
        )
        log.warning(
            "  Retired aged pending %s intent; broker confirms no client-id order",
            trade["ticker"],
        )
        return False

    log.warning(
        "  Pending %s entry remains unresolved (%s); leaving intent open",
        trade["ticker"],
        resolution,
    )
    return False


def _entry_capacity_is_unsettled(tc) -> bool:
    """Fail closed while an earlier entry can still consume cash or a slot.

    This check spans every strategy because a user may restart the singleton
    bot with a different strategy while an earlier market order is still live.
    Pending durable intents are resolved by client id before account sizing is
    loaded. Any active or unqueryable parent keeps new entries disabled for the
    cycle; filled parents are already represented in Alpaca account state.
    """
    try:
        trades = db_mod.get_open_trades()
    except Exception as exc:
        log.error(
            "New entries disabled: open entry capacity could not be verified (%s)",
            exc,
        )
        return True

    active = {
        "new",
        "accepted",
        "held",
        "pending_new",
        "partially_filled",
        "pending_replace",
        "pending_cancel",
    }
    terminal = {"filled", "closed", "canceled", "expired", "rejected"}

    blocked = False
    for trade in trades:
        if trade.get("entry_state") == "pending_submission":
            if not _resolve_pending_entry(tc, trade):
                blocked = True
                continue

        orders = _entry_order_candidates(tc, trade)
        statuses = {_status_str(order) for order in orders}
        if not statuses or any(status not in active | terminal for status in statuses):
            log.warning(
                "New entries disabled: %s entry state cannot be verified (trade %s)",
                trade.get("ticker"),
                trade.get("id"),
            )
            blocked = True
            continue
        if statuses & active:
            log.info(
                "New entries disabled: %s has an unsettled parent order (trade %s)",
                trade.get("ticker"),
                trade.get("id"),
            )
            blocked = True

    return blocked


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
    active = {"new", "accepted", "held", "pending_new", "partially_filled"}
    for o in _our_sell_orders(tc, trade["ticker"]):
        coid = str(getattr(o, "client_order_id", "") or "")
        if "-stop-" in coid and _status_str(o) in active:
            return o
    for entry in _entry_order_candidates(tc, trade):
        for leg in (getattr(entry, "legs", None) or []):
            if getattr(leg, "stop_price", None) is not None and _status_str(leg) in active:
                return leg
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

    qty = _position_qty(tc, trade["ticker"]) or float(getattr(stop, "qty", 0) or 0)
    if qty < 1:
        return
    stop_price_matches = (
        abs(float(getattr(stop, "stop_price", 0.0)) - target) < 1e-6
    )
    stop_qty_matches = (
        abs(float(getattr(stop, "qty", 0.0) or 0.0) - qty) < 1e-6
    )
    if stop_price_matches and stop_qty_matches:
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
        tc = _get_trading()
        entry_capacity_blocked = _entry_capacity_is_unsettled(tc)
        sizing_state = None if entry_capacity_blocked else _load_live_sizing(tc)

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

                # Persist ownership intent before any broker submission.
                entry_db_id = None
                entry_coid = None
                entry_accepted = False

                # Place order
                try:
                    if sizing_state is None:
                        log.info("  %s: entry disabled — no valid account sizing snapshot", ticker)
                        continue
                    if sizing_state.remaining_slots < 1:
                        log.info(
                            "  %s: entry skipped — %d-position account limit reached",
                            ticker,
                            PARAMS.max_concurrent_positions,
                        )
                        continue

                    snapshot = data_feed.fetch_snapshots([ticker]).get(ticker, {})
                    market_ref = float(snapshot.get("price") or 0)
                    if not math.isfinite(market_ref) or market_ref <= 0:
                        log.warning("  %s: no valid live price — skipping protected entry", ticker)
                        continue

                    size = whole_share_position_size(
                        sizing_state.equity,
                        sizing_state.remaining_cash,
                        market_ref,
                        PARAMS.position_size_pct,
                    )
                    qty = size.quantity
                    if qty < 1:
                        log.info(
                            "  %.0f%% equity budget $%.2f buys <1 whole share of "
                            "%s @ $%.2f — skipping (no order, no notify)",
                            PARAMS.position_size_pct * 100,
                            size.budget,
                            ticker,
                            market_ref,
                        )
                        continue

                    log.info(
                        "  Sizing %s: equity $%.2f, %.0f%% budget $%.2f, "
                        "cash $%.2f, ref $%.2f -> %d shares ($%.2f)",
                        ticker,
                        sizing_state.equity,
                        PARAMS.position_size_pct * 100,
                        sizing_state.equity * PARAMS.position_size_pct,
                        sizing_state.remaining_cash,
                        market_ref,
                        qty,
                        size.notional,
                    )

                    if strat_obj.exit_mode == "signal_with_stop":
                        sig.stop_loss = market_ref * (
                            1.0 - PARAMS.sma_cross_stop_loss_pct
                        )

                    entry_coid = _make_client_order_id(
                        strat_name, ticker, "entry"
                    )
                    entry_db_id = db_mod.save_trade(
                        ticker,
                        strat_name,
                        str(sig.date),
                        sig.entry_price,
                        sig.stop_loss,
                        sig.tp3,
                        shares=qty,
                        client_order_id=entry_coid,
                        alpaca_order_id=None,
                        entry_state="pending_submission",
                    )

                    def record_accepted_entry(accepted_info: dict) -> None:
                        nonlocal entry_accepted
                        entry_accepted = True
                        sizing_state.remaining_cash = max(
                            0.0, sizing_state.remaining_cash - size.notional
                        )
                        sizing_state.remaining_slots -= 1
                        if entry_db_id is not None:
                            try:
                                db_mod.set_entry_order_id(
                                    entry_db_id, accepted_info["alpaca_id"]
                                )
                            except Exception as update_exc:
                                # The durable client id is sufficient for
                                # ownership/recovery even if this enrichment
                                # fails; keep the protected entry tracked.
                                log.warning(
                                    "  %s entry id update failed; recovering by client id %s: %s",
                                    ticker,
                                    entry_coid,
                                    update_exc,
                                )

                    if strat_obj.exit_mode == "signal_with_stop":
                        info = _place_stop_only_entry(
                            tc,
                            ticker,
                            qty,
                            sig.stop_loss,
                            strat_name,
                            entry_coid=entry_coid,
                        )
                        record_accepted_entry(info)
                        coid, alpaca_id = info["entry_coid"], info["alpaca_id"]
                        log.info("  Stop-only OTO: %s x%d (SL $%.2f) [coid=%s]",
                                 ticker, qty, sig.stop_loss, coid)
                    elif qty >= 3:
                        # Atomic stop-protected entry, then three managed TP legs.
                        info = _place_scaled_entry(
                            tc,
                            ticker,
                            qty,
                            sig,
                            strat_name,
                            entry_coid=entry_coid,
                            on_entry_accepted=record_accepted_entry,
                        )
                        coid, alpaca_id = info["entry_coid"], info["alpaca_id"]
                        log.info("  Scaled entry: %s x%d (TP1 $%.2f / TP2 $%.2f / TP3 $%.2f, "
                                 "SL $%.2f) [coid=%s]", ticker, qty, sig.tp1, sig.tp2, sig.tp3,
                                 sig.stop_loss, coid)
                        if info.get("setup_error"):
                            log.error(
                                "  %s TP setup incomplete; atomic stop remains active: %s",
                                ticker,
                                info["setup_error"],
                            )
                            if not info.get("cleanup_confirmed", True):
                                # Unknown sell-order state makes every later
                                # allocation unsafe. Fail the rest of the entry
                                # cycle closed while the durable trade record and
                                # broker-held stop remain available to recovery.
                                sizing_state.remaining_cash = 0.0
                                sizing_state.remaining_slots = 0
                                log.error(
                                    "  %s TP cancellation unconfirmed; disabling later entries this cycle",
                                    ticker,
                                )
                            _reconcile_and_exit(strat_name, frames)
                    else:
                        # qty 1-2: too few shares to scale out — single OCO bracket at TP3
                        info = _place_single_bracket_entry(
                            tc,
                            ticker,
                            qty,
                            sig,
                            strat_name,
                            entry_coid=entry_coid,
                        )
                        record_accepted_entry(info)
                        coid, alpaca_id = info["entry_coid"], info["alpaca_id"]
                        log.info("  Single bracket (qty<3): %s x%d @ $%.2f (SL $%.2f / TP $%.2f) [coid=%s]",
                                 ticker, qty, sig.entry_price, sig.stop_loss, sig.tp3, coid)

                    orders_placed += 1
                    if strat_obj.has_take_profit:
                        body = (f"Entry ${sig.entry_price:.2f}\nSL ${sig.stop_loss:.2f}\n"
                                f"TP1 ${sig.tp1:.2f} / TP2 ${sig.tp2:.2f} / TP3 ${sig.tp3:.2f}\n"
                                f"Qty {qty}\nRef {coid}")
                        if info.get("setup_error"):
                            body += (
                                "\nWARNING: TP setup failed; broker stop remains active. "
                                f"{info['setup_error']}"
                            )
                    else:
                        body = (f"Daily SMA(50) cross entry ~${market_ref:.2f}\n"
                                f"SL ${sig.stop_loss:.2f}\nQty {qty}\nRef {coid}")
                    send_notification(f"Bot V2: {ticker} entry ({strat_name})", body)

                except Exception as e:
                    if entry_db_id is not None and not entry_accepted:
                        resolution, recovered = _lookup_entry_by_client_id(
                            tc, entry_coid
                        )
                        if resolution == "found":
                            recovered_info = {
                                "entry_coid": entry_coid,
                                "alpaca_id": str(
                                    getattr(recovered, "id", "") or ""
                                ),
                            }
                            record_accepted_entry(recovered_info)
                            orders_placed += 1
                            log.error(
                                "  %s submit response was ambiguous, but Alpaca accepted %s; durable entry adopted",
                                ticker,
                                recovered_info["alpaca_id"] or entry_coid,
                            )
                            send_notification(
                                f"Bot V2: {ticker} entry recovered ({strat_name})",
                                f"Alpaca accepted the protected entry despite a submit error.\n"
                                f"Qty {qty}\nRef {entry_coid}\nError: {e}",
                            )
                            _reconcile_and_exit(strat_name, frames)
                        elif _is_definite_submission_rejection(e):
                            try:
                                db_mod.close_trade(
                                    entry_db_id,
                                    datetime.now(timezone.utc).isoformat(),
                                    sig.entry_price,
                                    "entry_not_submitted",
                                    0,
                                    0.0,
                                    0.0,
                                    0.0,
                                    exit_client_order_id=entry_coid,
                                )
                            except Exception as close_exc:
                                log.error(
                                    "  Failed to close rejected entry intent for %s: %s",
                                    ticker,
                                    close_exc,
                                )
                        else:
                            sizing_state.remaining_cash = 0.0
                            sizing_state.remaining_slots = 0
                            log.error(
                                "  %s submission remains ambiguous (%s); intent stays pending and later entries are disabled",
                                ticker,
                                resolution,
                            )
                            _reconcile_and_exit(strat_name, frames)
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
            if not _resolve_pending_entry(tc, trade):
                continue
            try:
                pos = tc.get_open_position(ticker)
            except Exception:
                pos = None  # Alpaca raises when there is no position for the symbol

            # A previously submitted market exit owns this trade until Alpaca
            # confirms that it filled or reached a terminal unfilled state.
            # Never place a second sell while the first is still live/unknown.
            if trade.get("exit_alpaca_order_id") and _reconcile_pending_exit(tc, trade):
                continue

            if pos is None:
                if trade.get("exit_intent_reason") and _finalize_accumulated_exit(
                    trade, trade["exit_intent_reason"]
                ):
                    continue
                _reconcile_closed(tc, trade)
                continue

            # Durable intent survives a crash or ambiguous submit failure even
            # after the protective stop has already been canceled.
            if trade.get("exit_intent_reason"):
                _resume_exit_intent(tc, trade, pos)
                continue

            # Recover older crash-window orders created before intent persistence
            # existed. Owned client ids make those exits safely adoptable.
            if _adopt_untracked_exit(tc, trade):
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

    return _record_confirmed_exit(
        trade, exit_fill, _exit_reason_for_fill(trade, exit_fill)
    )


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


def _exit_progress_from_order(order, require_prefix: bool = False) -> dict | None:
    """Cumulative fill progress, including terminal partially-filled orders."""
    coid = str(getattr(order, "client_order_id", "") or "")
    if require_prefix and not coid.startswith(CLIENT_ORDER_PREFIX):
        return None
    order_id = str(getattr(order, "id", "") or "")
    qty = float(getattr(order, "filled_qty", 0) or 0)
    avg = getattr(order, "filled_avg_price", None)
    if not order_id or qty <= 0 or avg is None:
        return None
    price = float(avg)
    return {
        "price": price,
        "client_order_id": getattr(order, "client_order_id", None),
        "alpaca_order_id": order_id,
        "shares": qty,
        "notional": qty * price,
    }


def _exit_reason_for_fill(trade: dict, fill: dict) -> str:
    """Infer why a broker fill closed the trade from the owned order id."""
    coid = str(fill.get("client_order_id") or "")
    strat_obj = REGISTRY.get(trade.get("strategy", ""))
    if "-exit-" in coid:
        return (
            "sma_cross_down"
            if strat_obj is not None and strat_obj.exit_mode == "signal_with_stop"
            else "time_stop"
        )
    return (
        "stop_loss"
        if strat_obj is not None and strat_obj.exit_mode == "signal_with_stop"
        else "bracket_filled"
    )


def _finalize_accumulated_exit(
    trade: dict, reason: str, reference_fill: dict | None = None
) -> bool:
    """Close once durable broker fills cover the bot-owned share quantity."""
    ticker = trade["ticker"]
    entry = float(trade["entry_price"])
    try:
        total_shares, total_notional = db_mod.get_exit_fill_totals(trade["id"])
        if total_shares <= 0:
            return False
        owned_shares = float(trade.get("shares") or 0)
        if owned_shares > 0 and total_shares < owned_shares - 1e-9:
            return False
        exit_price = total_notional / total_shares
        pnl = total_notional - entry * total_shares
        pnl_pct = (exit_price - entry) / entry if entry else 0.0
        reference_fill = reference_fill or {}
        db_mod.close_trade(
            trade["id"],
            datetime.now(timezone.utc).isoformat(),
            exit_price,
            reason,
            _days_held(trade["entry_date"]),
            total_shares,
            pnl,
            pnl_pct,
            exit_client_order_id=(
                reference_fill.get("client_order_id")
                or trade.get("exit_client_order_id")
            ),
            exit_alpaca_order_id=(
                reference_fill.get("alpaca_order_id")
                or trade.get("exit_alpaca_order_id")
            ),
        )
    except Exception as e:
        log.error("  Confirmed %s exit could not be recorded: %s", ticker, e)
        return False

    log.info(
        "  Reconciled exit: %s closed @ $%.2f (%s) pnl=$%.2f",
        ticker,
        exit_price,
        reason,
        pnl,
    )
    send_notification(
        f"Bot V2: {ticker} exit ({trade['strategy']})",
        f"Filled x{total_shares:g} @ ${exit_price:.2f}\nReason {reason}\n"
        f"PnL ${pnl:.2f}\nRef "
        f"{reference_fill.get('client_order_id') or trade.get('exit_client_order_id') or 'n/a'}",
    )
    return True


def _record_confirmed_exit(trade: dict, fill: dict, reason: str) -> bool:
    """Idempotently record a broker fill, then finalize if owned shares are done."""
    try:
        shares = float(fill.get("shares") or 0)
        db_mod.record_exit_order_progress(
            trade["id"],
            fill.get("alpaca_order_id") or "",
            fill.get("client_order_id"),
            shares,
            float(fill.get("notional") or shares * float(fill["price"])),
        )
    except Exception as e:
        log.error("  Confirmed %s exit fill could not be recorded: %s", trade["ticker"], e)
        return False
    return _finalize_accumulated_exit(trade, reason, fill)


def _reconcile_pending_exit(tc, trade: dict) -> bool:
    """Reconcile a submitted exit; True means no new exit may be placed now."""
    exit_id = trade.get("exit_alpaca_order_id")
    exit_coid = trade.get("exit_client_order_id")
    if not exit_id:
        return False

    try:
        order = tc.get_order_by_id(exit_id)
    except Exception as e:
        log.warning(
            "  %s pending exit status unavailable (%s) — blocking duplicate sell",
            trade["ticker"],
            e,
        )
        return True

    progress = _exit_progress_from_order(order, require_prefix=True)
    if progress is not None:
        db_mod.record_exit_order_progress(
            trade["id"],
            progress["alpaca_order_id"],
            progress["client_order_id"],
            progress["shares"],
            progress["notional"],
        )

    status = _status_str(order)
    if status in ("filled", "closed"):
        if progress is None:
            log.warning("  %s exit reports %s without fill details", trade["ticker"], status)
            return True
        completed = _record_confirmed_exit(
            trade, progress, trade.get("exit_intent_reason") or _exit_reason_for_fill(trade, progress)
        )
        if not completed:
            db_mod.clear_exit_pending(trade["id"])
            log.warning(
                "  %s filled exit covered only part of the owned quantity; retained intent",
                trade["ticker"],
            )
        return True

    if status in ("canceled", "expired", "rejected"):
        reason = trade.get("exit_intent_reason") or _exit_reason_for_fill(
            trade,
            progress or {"client_order_id": exit_coid},
        )
        if _finalize_accumulated_exit(trade, reason, progress):
            return True
        db_mod.clear_exit_pending(trade["id"])
        log.warning(
            "  %s exit order %s ended %s; recorded any partial fill and retained intent",
            trade["ticker"],
            exit_id or exit_coid,
            status,
        )
        return True

    return True


def _adopt_untracked_exit(tc, trade: dict) -> bool:
    """Adopt an owned exit submitted before its DB pending write completed."""
    if trade.get("exit_alpaca_order_id") or trade.get("exit_client_order_id"):
        return False

    marker = f"{CLIENT_ORDER_PREFIX}-exit-{trade['strategy']}-{trade['ticker']}-"
    for order in _our_sell_orders(tc, trade["ticker"]):
        coid = str(getattr(order, "client_order_id", "") or "")
        if not coid.startswith(marker) or not _order_is_after_trade(order, trade):
            continue

        fill = _exit_fill_from_order(order, require_prefix=True)
        if fill is not None:
            if not db_mod.exit_order_already_used(fill["alpaca_order_id"]):
                _record_confirmed_exit(
                    trade, fill, _exit_reason_for_fill(trade, fill)
                )
                return True
            continue

        if _status_str(order) in ("canceled", "expired", "rejected"):
            continue

        exit_id = str(getattr(order, "id", "") or "")
        try:
            db_mod.set_exit_pending(trade["id"], coid, exit_id)
            log.warning(
                "  Adopted untracked pending exit %s for %s after restart",
                exit_id or coid,
                trade["ticker"],
            )
        except Exception as e:
            log.error(
                "  %s owned exit found but pending DB state still failed: %s",
                trade["ticker"],
                e,
            )
        return True

    return False


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


def _cancel_attached_signal_stop(tc, trade: dict) -> tuple[bool, list]:
    """Cancel and confirm the stop attached to a signal-exit OTO entry.

    This is deliberately fail-closed: an unconfirmed stop can still reserve or
    sell the shares, so the crossover market sell must not race it.
    """
    from alpaca.trading.requests import GetOrderByIdRequest

    active = {"new", "accepted", "held", "pending_new", "partially_filled"}
    canceling = {"pending_cancel"}
    safely_inactive = {"canceled", "expired", "rejected"}
    entry_id = trade.get("alpaca_order_id")
    if not entry_id:
        log.error("  %s cross exit blocked: entry order id is missing", trade["ticker"])
        return False, []

    def nested_entry():
        return tc.get_order_by_id(
            entry_id, filter=GetOrderByIdRequest(nested=True)
        )

    try:
        entry_order = nested_entry()
    except Exception as e:
        log.error(
            "  %s cross exit blocked: attached stop could not be inspected: %s",
            trade["ticker"],
            e,
        )
        return False, []

    legs = list(getattr(entry_order, "legs", None) or [])
    if not legs:
        log.error(
            "  %s cross exit blocked: OTO entry has no visible attached stop",
            trade["ticker"],
        )
        return False, []

    leg_ids = {str(getattr(leg, "id", "") or "") for leg in legs}
    if "" in leg_ids:
        log.error("  %s cross exit blocked: attached stop id is missing", trade["ticker"])
        return False, []

    for leg in legs:
        status = _status_str(leg)
        if status in ("filled", "closed"):
            log.warning(
                "  %s cross exit blocked: attached stop %s is already %s",
                trade["ticker"],
                leg.id,
                status,
            )
            return False, [leg]
        if status in safely_inactive:
            continue
        if status in canceling:
            continue
        if status not in active:
            log.error(
                "  %s cross exit blocked: attached stop %s has unknown status %s",
                trade["ticker"],
                leg.id,
                status,
            )
            return False, legs
        try:
            tc.cancel_order_by_id(leg.id)
            log.info("  Requested cancellation of attached stop %s for %s", leg.id, trade["ticker"])
        except Exception as e:
            log.error(
                "  %s cross exit blocked: attached stop %s could not be canceled: %s",
                trade["ticker"],
                leg.id,
                e,
            )
            return False, legs

    # Alpaca cancellation is asynchronous. Refetch the nested parent until every
    # leg is terminal, with a short bounded wait; otherwise leave the position alone.
    confirmed_legs = legs
    for attempt in range(5):
        try:
            confirmed_legs = [tc.get_order_by_id(leg_id) for leg_id in leg_ids]
            statuses = {
                str(getattr(leg, "id", "") or ""): _status_str(leg)
                for leg in confirmed_legs
            }
        except Exception as e:
            log.error(
                "  %s cross exit blocked: stop cancellation could not be confirmed: %s",
                trade["ticker"],
                e,
            )
            return False, []

        if all(statuses.get(leg_id) in safely_inactive for leg_id in leg_ids):
            return True, confirmed_legs
        if any(statuses.get(leg_id) in ("filled", "closed") for leg_id in leg_ids):
            return False, confirmed_legs
        if attempt < 4:
            time.sleep(0.2)

    log.error(
        "  %s cross exit blocked: attached stop cancellation was not confirmed",
        trade["ticker"],
    )
    return False, confirmed_legs


def _cancel_owned_legs_best_effort(tc, trade: dict):
    """Release legacy bracket shares without weakening their existing behavior."""
    try:
        from alpaca.trading.requests import GetOrderByIdRequest
        entry_id = trade.get("alpaca_order_id")
        if not entry_id:
            return
        entry_order = tc.get_order_by_id(
            entry_id, filter=GetOrderByIdRequest(nested=True)
        )
        for leg in (getattr(entry_order, "legs", None) or []):
            if _status_str(leg) in (
                "new", "accepted", "held", "pending_new", "partially_filled"
            ):
                try:
                    tc.cancel_order_by_id(leg.id)
                    log.info("  Cancelled bracket leg %s for %s", leg.id, trade["ticker"])
                except Exception as e:
                    log.debug("  Could not cancel leg %s: %s", getattr(leg, "id", "?"), e)
    except Exception as e:
        log.debug("  Could not inspect bracket legs for %s: %s", trade["ticker"], e)


def _account_order_progress(trade: dict, order, require_prefix: bool = False) -> dict | None:
    progress = _exit_progress_from_order(order, require_prefix=require_prefix)
    if progress is not None:
        db_mod.record_exit_order_progress(
            trade["id"],
            progress["alpaca_order_id"],
            progress["client_order_id"],
            progress["shares"],
            progress["notional"],
        )
    return progress


def _execute_exit_intent(
    tc, trade: dict, pos, reason: str, exit_coid: str
) -> bool:
    """Cancel protection, refresh quantity, and submit the persisted intent."""
    ticker = trade["ticker"]
    strat_obj = REGISTRY.get(trade.get("strategy", ""))
    stop_progress: list[dict] = []

    if strat_obj is not None and strat_obj.exit_mode == "signal_with_stop":
        safe_to_sell, stop_orders = _cancel_attached_signal_stop(tc, trade)
        for stop_order in stop_orders:
            progress = _account_order_progress(trade, stop_order)
            if progress is not None:
                stop_progress.append(progress)
        if stop_progress and _finalize_accumulated_exit(
            trade, "stop_loss", stop_progress[-1]
        ):
            return False
        if not safe_to_sell:
            # A fully filled stop may have finished the exit while cancellation
            # was being attempted. Record it, but never race it with a market sell.
            try:
                tc.get_open_position(ticker)
            except Exception:
                if stop_progress:
                    _record_confirmed_exit(trade, stop_progress[-1], "stop_loss")
            return False
    else:
        _cancel_owned_legs_best_effort(tc, trade)

    # Cancellation is asynchronous, so the position used to decide the exit may
    # now be stale. Refetch and subtract every durable partial fill before sizing.
    try:
        live_pos = tc.get_open_position(ticker)
    except Exception:
        live_pos = None

    filled_shares, _filled_notional = db_mod.get_exit_fill_totals(trade["id"])
    our_qty = float(trade.get("shares") or 0)
    remaining_ours = max(0.0, our_qty - filled_shares) if our_qty > 0 else 0.0
    if live_pos is None:
        if _finalize_accumulated_exit(
            trade, reason, stop_progress[-1] if stop_progress else None
        ):
            return False
        _reconcile_closed(tc, trade)
        return False

    pos_qty = abs(float(live_pos.qty))
    qty_to_close = int(min(remaining_ours, pos_qty)) if our_qty > 0 else int(pos_qty)
    if qty_to_close < 1:
        if _finalize_accumulated_exit(
            trade, reason, stop_progress[-1] if stop_progress else None
        ):
            return False
        log.warning(
            "  %s: no remaining owned shares to close (filled=%s pos=%s)",
            ticker,
            filled_shares,
            pos_qty,
        )
        return False

    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        sell = MarketOrderRequest(
            symbol=ticker,
            qty=qty_to_close,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            client_order_id=exit_coid,
        )
        order = tc.submit_order(sell)
        exit_id = str(getattr(order, "id", "") or "")
        log.info(
            "  Submitted bot exit %s x%d (%s) [coid=%s]",
            ticker,
            qty_to_close,
            reason,
            exit_coid,
        )
    except Exception as e:
        # Intent and client id were persisted before protection was removed.
        # The same unique client id is retried, so an ambiguous broker response
        # cannot create two exit orders.
        log.error("  Failed to submit %s exit; durable intent retained: %s", ticker, e)
        return False

    try:
        db_mod.set_exit_pending(trade["id"], exit_coid, exit_id)
    except Exception as e:
        log.error("  %s exit submitted but pending DB state failed: %s", ticker, e)

    progress = _account_order_progress(trade, order, require_prefix=True)
    if _status_str(order) in ("filled", "closed") and progress is not None:
        _record_confirmed_exit(trade, progress, reason)
    return True


def _resume_exit_intent(tc, trade: dict, pos) -> bool:
    """Resume a durable exit regardless of whether the latest bars still cross."""
    reason = trade.get("exit_intent_reason")
    if not reason:
        return False
    exit_coid = trade.get("exit_client_order_id")
    if not exit_coid:
        exit_coid = _make_client_order_id(trade["strategy"], trade["ticker"], "exit")
        try:
            db_mod.set_exit_intent(trade["id"], reason, exit_coid)
        except Exception as e:
            log.error("  %s exit intent could not be refreshed: %s", trade["ticker"], e)
            return True

    # A submit may have succeeded just before the process lost its response or
    # before SQLite stored the Alpaca id. Adopt it by the persisted unique id.
    try:
        existing = tc.get_order_by_client_id(exit_coid)
    except Exception:
        existing = None
    if existing is not None:
        exit_id = str(getattr(existing, "id", "") or "")
        db_mod.set_exit_pending(trade["id"], exit_coid, exit_id)
        pending_trade = dict(
            trade,
            exit_client_order_id=exit_coid,
            exit_alpaca_order_id=exit_id,
        )
        _reconcile_pending_exit(tc, pending_trade)
        return True

    intent_trade = dict(
        trade,
        exit_client_order_id=exit_coid,
        exit_intent_reason=reason,
    )
    return _execute_exit_intent(tc, intent_trade, pos, reason, exit_coid)


def _close_owned(tc, trade: dict, pos, reason: str) -> bool:
    """Persist exit intent before changing protection, then execute it."""
    ticker = trade["ticker"]
    if (
        trade.get("exit_intent_reason")
        or trade.get("exit_alpaca_order_id")
        or trade.get("exit_client_order_id")
    ):
        log.warning("  %s already has an exit lifecycle — duplicate sell blocked", ticker)
        return False

    exit_coid = _make_client_order_id(trade["strategy"], ticker, "exit")
    try:
        db_mod.set_exit_intent(trade["id"], reason, exit_coid)
    except Exception as e:
        log.error("  %s exit blocked: intent could not be persisted: %s", ticker, e)
        return False

    intent_trade = dict(
        trade,
        exit_client_order_id=exit_coid,
        exit_intent_reason=reason,
    )
    return _execute_exit_intent(tc, intent_trade, pos, reason, exit_coid)


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
