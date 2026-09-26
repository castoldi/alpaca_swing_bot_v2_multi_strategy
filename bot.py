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
import json
import math
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, date, timezone, timedelta, time as dtime
from zoneinfo import ZoneInfo
from pathlib import Path

import pandas as pd

from config import PARAMS, TICKERS, LEVERAGED_TICKERS, ALPACA_KEY, ALPACA_SECRET, ALPACA_PAPER, StrategyType, BAR_TIMEFRAME
from logger_setup import get_logger
from strategies import (REGISTRY, add_indicators, is_tp_reachable_in_days,
                        strategy_universe, add_earnings_filter, SKIP_EARNINGS_STRATEGIES)
from dashboard import db as db_mod
from dashboard import bot_hooks
from notifier import send_notification
import broker_sync
import data_feed
import portfolio
import runtime
import tax as tax_mod
from position_sizing import (combine_headroom, group_headroom, leveraged_headroom,
                             whole_share_position_size)

log = get_logger(__name__)
ROOT = Path(__file__).parent
SERVICE = "bot"  # name used for run/bot.pid, run/bot.meta.json, run/bot.heartbeat

# Every order this bot places carries a client_order_id beginning with this prefix.
# It is our correlation id with Alpaca and the proof of ownership: the bot will only
# ever close a position whose originating order carries this prefix.
CLIENT_ORDER_PREFIX = "swingv2"
ENTRY_PENDING_GRACE = timedelta(minutes=5)
# Broker states of an entry order that is still working normally.
_WORKING_ENTRY_STATES = {"new", "accepted", "pending_new", "partially_filled", "held"}


class EntryPending(ValueError):
    """The owned entry order is still working, so its quantity is not final.

    A ValueError subclass so every existing fail-closed caller still treats it
    as unsettled; only the reconciler tells a young pending entry (normal right
    after submission, especially at the open) from a stuck one.
    """

    def __init__(self, message: str, submitted_at=None):
        super().__init__(message)
        self.submitted_at = submitted_at


@dataclass
class LiveSizingState:
    equity: float
    remaining_cash: float
    remaining_slots: int
    # Market value of every open leveraged-ETF position, whoever opened it —
    # the cap is about account risk, not about which orders the bot owns.
    leveraged_notional: float = 0.0
    # Market value per symbol, account-wide, for the correlated-group caps.
    open_notional_by_ticker: dict[str, float] = field(default_factory=dict)


def _daily_loss_pct(account) -> float | None:
    """Fractional loss vs yesterday's closing equity, or None when unknown."""
    try:
        last = float(getattr(account, "last_equity", None))
        equity = float(account.equity)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(last) and last > 0 and math.isfinite(equity)):
        return None
    return (last - equity) / last


def _bot_daily_loss_pct(account) -> float | None:
    """This bot's OWN loss since the previous session close, or None if unknown.

    The paper key is shared with other projects, so account equity moves on
    trades this bot never made (and can hide this bot's losses). Numerator: P&L
    of the bot's open trades plus trades closed today, each measured from the
    previous close (or from its fill if it entered today). Denominator: the
    capital allocation (capped at yesterday's account equity) — the same base
    the 20% position sizing uses; yesterday's account equity when the
    allocation is off. The backtest guard measures the same thing on its
    isolated portfolio.
    """
    try:
        base = float(getattr(account, "last_equity", None))
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(base) and base > 0):
        return None
    allocation = PARAMS.bot_capital_allocation
    if math.isfinite(allocation) and allocation > 0:
        base = min(base, allocation)
    today = datetime.now(_ET).date()
    try:
        relevant = []
        for trade in db_mod.get_trades_for_ledger():
            if not portfolio.is_real_trade(trade) or trade.get("entry_state") == "pending_submission":
                continue
            if portfolio.is_open(trade):
                relevant.append(trade)
            elif portfolio.is_closed(trade):
                exited = _parse_timestamp(trade.get("exit_date"))
                if exited is not None and exited.astimezone(_ET).date() == today:
                    relevant.append(trade)
        if not relevant:
            return 0.0
        snapshots = data_feed.fetch_snapshots(sorted({str(t["ticker"]) for t in relevant}))
        pnl = 0.0
        for trade in relevant:
            snap = snapshots.get(str(trade["ticker"])) or {}
            entered = (_parse_timestamp(trade.get("entry_filled_at"))
                       or _parse_timestamp(trade.get("created_at")))
            if entered is None:
                return None
            reference = (float(snap.get("prev_close") or 0)
                         if entered.astimezone(_ET).date() < today
                         else portfolio.effective_entry_price(trade))
            current = (float(snap.get("price") or 0) if portfolio.is_open(trade)
                       else float(trade.get("exit_price") or 0))
            if not (math.isfinite(reference) and reference > 0
                    and math.isfinite(current) and current > 0):
                return None
            pnl += (current - reference) * portfolio.shares_of(trade)
        return -pnl / base
    except Exception as exc:
        log.warning("Bot-owned daily loss unavailable (%s)", exc)
        return None


_KILL_SWITCH_MARKER = ROOT / "run" / "killswitch.date"


def _announce_kill_switch(loss_pct: float, equity: float) -> None:
    """Log every cycle, but email only once per trading day."""
    log.error(
        "KILL SWITCH: daily loss %.2f%% >= %.2f%% limit — new entries disabled (equity $%.2f)",
        loss_pct * 100,
        PARAMS.max_daily_loss_pct * 100,
        equity,
    )
    today = datetime.now(_ET).date().isoformat()
    try:
        if (
            _KILL_SWITCH_MARKER.exists()
            and _KILL_SWITCH_MARKER.read_text().strip() == today
        ):
            return
        _KILL_SWITCH_MARKER.parent.mkdir(exist_ok=True)
        _KILL_SWITCH_MARKER.write_text(today)
    except Exception:
        pass  # a marker failure must never suppress the alert itself
    send_notification(
        "Bot V2: kill switch tripped",
        f"Daily loss {loss_pct * 100:.2f}% breached the "
        f"{PARAMS.max_daily_loss_pct * 100:.1f}% limit.\n"
        f"New entries are disabled for the rest of the day. Open positions "
        f"keep their broker-held stop/TP protection and exits keep running.\n"
        f"Equity ${equity:.2f}",
    )


def _open_leveraged_notional(positions) -> float:
    """Market value of open leveraged-ETF positions.

    Fails closed: a position whose value cannot be read is charged its full
    cap, so an unreadable position blocks further leveraged entries rather than
    silently freeing headroom.
    """
    leveraged = set(LEVERAGED_TICKERS)
    total = 0.0
    for pos in positions or []:
        symbol = str(getattr(pos, "symbol", "") or "")
        if symbol not in leveraged:
            continue
        try:
            value = abs(float(getattr(pos, "market_value", None)))
            if not math.isfinite(value):
                raise ValueError("non-finite market value")
            total += value
        except (TypeError, ValueError):
            log.warning(
                "  %s: market value unreadable — treating as full leveraged cap",
                symbol,
            )
            return float("inf")
    return total


def _open_notional_by_ticker(positions) -> dict[str, float]:
    """Market value per open symbol; an unreadable value counts as infinite,
    so the exposure-group cap fails closed exactly like the leveraged cap."""
    out: dict[str, float] = {}
    for pos in positions or []:
        symbol = str(getattr(pos, "symbol", "") or "")
        if not symbol:
            continue
        try:
            value = abs(float(getattr(pos, "market_value", None)))
            if not math.isfinite(value):
                raise ValueError("non-finite market value")
        except (TypeError, ValueError):
            value = float("inf")
        out[symbol] = out.get(symbol, 0.0) + value
    return out


def _bot_owned_symbols() -> set[str] | None:
    """Symbols this bot currently tracks as open, or None when unreadable."""
    try:
        return {
            str(trade.get("ticker") or "")
            for trade in (db_mod.get_open_trades() or [])
        }
    except Exception as exc:
        log.warning(
            "Position ownership unavailable (%s) — charging every account "
            "position to this bot",
            exc,
        )
        return None


def _our_open_position_count(positions, owned: set[str] | None) -> int:
    """How many open account positions belong to this bot.

    The Alpaca key may be shared with other bots, so the raw account position
    count is not this bot's slot usage: another bot's symbols would silently
    consume `max_concurrent_positions` and, at five of them, stop this bot
    entering anything while still looking healthy.

    Fails closed — when ownership cannot be determined every position is
    charged to this bot, costing capacity rather than risking an over-allocation.
    """
    if owned is None:
        return len(positions or [])
    return sum(
        1
        for pos in (positions or [])
        if str(getattr(pos, "symbol", "") or "") in owned
    )


def _tax_entry_block(ticker: str) -> str | None:
    """Year-end wash-sale reason to skip this entry, or None.

    Never fails an entry on its own error: tax deferral is an optimisation, not
    a safety rule, so an unreadable history logs and allows the trade.
    """
    try:
        return tax_mod.year_end_entry_block(
            ticker,
            datetime.now(timezone.utc),
            db_mod.get_closed_trades(limit=400),
            guard_start_month=PARAMS.tax_guard_start_month,
            guard_start_day=PARAMS.tax_guard_start_day,
            enabled=PARAMS.tax_year_end_guard,
            hard_block=PARAMS.tax_hard_block,
            hard_block_days=PARAMS.tax_hard_block_days,
            mtm_475f=PARAMS.tax_mtm_475f,
            crypto_symbols=PARAMS.tax_crypto_symbols,
        )
    except Exception as exc:
        log.warning("  %s: tax guard skipped (%s)", ticker, exc)
        return None


def _refresh_tax_records() -> None:
    """Recompute tax records after exits. Never interrupts trading."""
    try:
        db_mod.rebuild_tax_records()
    except Exception as exc:
        log.warning("Tax records not refreshed (%s)", exc)


def _bot_open_cost_basis() -> float:
    """Dollars this bot has in its own open trades (shares x actual fill).

    Raises when the ledger is unreadable or a row is malformed, so the caller
    disables entries rather than assuming the allocation is free.
    """
    total = 0.0
    for trade in db_mod.get_open_trades() or []:
        shares = float(trade.get("shares") or 0)
        price = portfolio.effective_entry_price(trade)
        if not (math.isfinite(shares) and math.isfinite(price)) or shares < 0 or price < 0:
            raise ValueError(f"unreadable cost basis for trade {trade.get('id')}")
        total += shares * price
    return total


def _allocation_sizing(equity: float, cash: float) -> tuple[float, float]:
    """(sizing base, spendable cash) under the virtual capital allocation.

    The base is the allocation, never more than the real account equity. Cash
    is the smaller of real account cash (another project may have spent it)
    and what is left of the allocation after this bot's own open positions.
    With the allocation off (<= 0) both stay account-wide. An unreadable
    ledger fails closed: nothing is spendable this cycle.
    """
    allocation = PARAMS.bot_capital_allocation
    if not (math.isfinite(allocation) and allocation > 0):
        return equity, cash
    try:
        deployed = _bot_open_cost_basis()
    except Exception as exc:
        log.warning("Allocation usage unreadable (%s) — no new entries this cycle", exc)
        return min(allocation, equity), 0.0
    return min(allocation, equity), min(cash, max(0.0, allocation - deployed))


def _load_live_sizing(tc) -> LiveSizingState | None:
    """Read one safe, non-margin sizing snapshot for the current bot cycle.

    Sizing runs against ``PARAMS.bot_capital_allocation`` (see
    ``_allocation_sizing``) so this bot's footprint on the shared key is
    bounded. Leveraged/group notional stays account-wide on purpose — exposure
    is exposure whoever opened it. Only the position-slot count is scoped to
    what this bot owns.
    """
    try:
        account = tc.get_account()
        account_equity = float(account.equity)
        account_cash = float(account.cash)
        if (
            not math.isfinite(account_equity)
            or not math.isfinite(account_cash)
            or account_equity <= 0
            or account_cash < 0
        ):
            raise ValueError("invalid equity or cash")
        equity, cash = _allocation_sizing(account_equity, account_cash)
        if equity != account_equity or cash != account_cash:
            log.info(
                "Sizing base: allocation $%.2f (account equity $%.2f), "
                "spendable $%.2f (account cash $%.2f)",
                equity, account_equity, cash, account_cash,
            )
        get_positions = getattr(tc, "get_all_positions", None)
        positions = get_positions() if callable(get_positions) else []
        open_position_count = _our_open_position_count(
            positions, _bot_owned_symbols()
        )
        if (
            not math.isfinite(equity)
            or not math.isfinite(cash)
            or equity <= 0
            or cash < 0
            or open_position_count < 0
        ):
            raise ValueError("invalid equity or cash")
        loss_pct = _bot_daily_loss_pct(account)
        if loss_pct is None:
            # Unknown own P&L: fall back to the account-wide drop, which can
            # only block entries (conservative), never permit extra risk.
            loss_pct = _daily_loss_pct(account)
            if loss_pct is not None:
                log.warning("Kill switch using account-wide equity (bot-owned P&L unavailable)")
        if loss_pct is not None and loss_pct >= PARAMS.max_daily_loss_pct:
            _announce_kill_switch(loss_pct, equity)
            return None
        return LiveSizingState(
            equity=equity,
            remaining_cash=cash,
            remaining_slots=max(
                0, PARAMS.max_concurrent_positions - open_position_count
            ),
            leveraged_notional=_open_leveraged_notional(positions),
            open_notional_by_ticker=_open_notional_by_ticker(positions),
        )
    except Exception as exc:
        log.warning("New entries disabled this cycle: account sizing unavailable (%s)", exc)
        return None


def _make_client_order_id(strategy: str, ticker: str, kind: str) -> str:
    """Unique, Alpaca-safe correlation id, e.g. swingv2-entry-ensemble-ARM-9f3a1c2b."""
    return f"{CLIENT_ORDER_PREFIX}-{kind}-{strategy}-{ticker}-{uuid.uuid4().hex[:8]}"


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
        time_in_force=TimeInForce.GTC,
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
        time_in_force=TimeInForce.GTC,
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


def _place_protective_oco(
    tc,
    trade: dict,
    qty: float,
    stop_price: float,
    take_profit: float | None,
) -> dict:
    """Submit GTC OCO or a standalone stop after confirming old protection dead.

    Persist the client id BEFORE submitting. An ambiguous response leaves that
    intent in place for adoption; it must never trigger a fresh submission.
    """
    from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
    from alpaca.trading.requests import (
        LimitOrderRequest,
        StopOrderRequest,
        StopLossRequest,
        TakeProfitRequest,
    )

    coid = _make_client_order_id(trade["strategy"], trade["ticker"], "protect")
    common = dict(
        symbol=trade["ticker"],
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        client_order_id=coid,
        extended_hours=False,
    )
    if take_profit is None:
        request = StopOrderRequest(**common, stop_price=round(stop_price, 2))
    else:
        request = LimitOrderRequest(
            **common, order_class=OrderClass.OCO,
            limit_price=round(take_profit, 2),
            take_profit=TakeProfitRequest(limit_price=round(take_profit, 2)),
            stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
        )
    previous = (trade.get("protect_client_order_id"), trade.get("protect_alpaca_order_id"))
    db_mod.set_protect_order_ids(trade["id"], coid, None)
    trade.update(protect_client_order_id=coid, protect_alpaca_order_id=None)
    try:
        order = tc.submit_order(request)
    except Exception as exc:
        resolution, order = _lookup_entry_by_client_id(tc, coid)
        if resolution != "found":
            if resolution == "not_found" and _is_definite_submission_rejection(exc):
                # An explicit rejection plus no matching order permits a later
                # attempt. A timeout/5xx/unknown lookup never does.
                db_mod.set_protect_order_ids(trade["id"], *previous)
                trade.update(protect_client_order_id=previous[0], protect_alpaca_order_id=previous[1])
            raise
    alpaca_id = str(getattr(order, "id", "") or "")
    if (not alpaca_id or order.client_order_id != coid
            or order.symbol != trade["ticker"]
            or getattr(order.side, "value", order.side) != "sell"):
        raise ValueError("protective submission returned mismatched ownership")
    db_mod.set_protect_order_ids(trade["id"], coid, alpaca_id)
    trade.update(protect_client_order_id=coid, protect_alpaca_order_id=alpaca_id)
    return {"order": order, "protect_coid": coid, "alpaca_id": alpaca_id}


def _ensure_owned_protection(tc, trade: dict) -> None:
    """Repair expired, DAY, or wrong-quantity protection using exact owned links.

    Alpaca activates bracket/OTO children only after the entry fills. OCO has
    the limit TP as its parent and the stop as its child. GTC still has a 90-day
    expiry, so coverage is checked on every reconciliation, not just entry.
    """
    if trade.get("exit_intent_reason") or trade.get("exit_alpaca_order_id"):
        return
    orders = _owned_bracket_orders(tc, trade)
    if _record_bracket_progress(trade, orders):
        return
    remaining = float(trade["shares"]) - db_mod.get_exit_fill_totals(trade["id"])[0]
    terminal = {"filled", "closed", "canceled", "expired", "rejected"}
    live = [o for o in orders if _status_str(o) not in terminal]
    healthy = []
    for item in live:
        if _status_str(item) not in {"new", "accepted", "partially_filled", "held", "done_for_day"}:
            # Transitional/unknown states cannot prove settled coverage.
            healthy.append(False)
            continue
        qty = float(item.qty) - float(item.filled_qty)
        tif = getattr(item.time_in_force, "value", item.time_in_force)
        healthy.append(math.isfinite(qty) and abs(qty - remaining) < 1e-8 and tif == "gtc")
    kinds = sorted(str(getattr(getattr(o, "type", ""), "value", getattr(o, "type", ""))) for o in live)
    stop_only = REGISTRY[trade["strategy"]].exit_mode == "signal_with_stop"
    expected = ["stop"] if stop_only else ["limit", "stop"]
    if kinds == expected and all(healthy):
        return

    if not _cancel_owned_bracket(tc, trade):
        return
    # Account quantity is only a cap. Re-read after asynchronous cancellation,
    # then subtract all recorded fills, including fills racing the cancellation.
    remaining = float(trade["shares"]) - db_mod.get_exit_fill_totals(trade["id"])[0]
    pos = tc.get_open_position(trade["ticker"])
    account_qty = float(pos.qty)
    available = float(pos.qty_available) if getattr(pos, "qty_available", None) is not None else account_qty
    if (not math.isfinite(remaining) or remaining < 1 or not remaining.is_integer()
            or not math.isfinite(account_qty) or account_qty < remaining
            or not math.isfinite(available) or available < remaining):
        raise ValueError("remaining owned whole shares are unavailable for protection")
    current = float(pos.current_price)
    stop = round(float(trade["stop_loss"]), 2)
    tp = None if stop_only else round(float(trade["take_profit"]), 2)
    if (not math.isfinite(current) or current <= 0 or not math.isfinite(stop) or stop <= 0
            or (tp is not None and (not math.isfinite(tp) or tp <= stop))):
        raise ValueError("invalid protection prices")
    # Alpaca requires an advanced sell stop at least $0.01 below the market
    # and TP base prices. Do not move the strategy's stop to force acceptance.
    if current < stop + 0.01 or (tp is not None and (current >= tp or tp < stop + 0.01)):
        _close_owned(tc, trade, pos, reason="protection_breached")
        return
    _place_protective_oco(tc, trade, remaining, stop, tp)
    log.warning("  %s: restored owned GTC protection for %g shares", trade["ticker"], remaining)


def _effective_entry_price(trade: dict) -> float:
    """Real broker fill when recorded, else the signal-close entry price."""
    return float(trade.get("entry_filled_price") or trade["entry_price"])


def _record_entry_fill(
    tc, trade_id, alpaca_id, *, attempts: int = 4, delay: float = 0.5
) -> None:
    """Briefly poll the entry order and persist its real average fill price.

    Market entries usually fill in under a second during market hours; if the
    fill arrives later, _backfill_entry_fill picks it up on reconciliation.
    """
    if not trade_id or not alpaca_id:
        return
    for attempt in range(attempts):
        try:
            order = tc.get_order_by_id(alpaca_id)
        except Exception:
            return
        avg = getattr(order, "filled_avg_price", None)
        if _status_str(order) in ("filled", "closed", "canceled", "expired", "rejected"):
            try:
                price = float(avg or 0)
                qty = float(getattr(order, "filled_qty", 0) or 0)
                if not (math.isfinite(price) and price > 0 and math.isfinite(qty) and qty > 0):
                    return
                db_mod.set_entry_fill(
                    trade_id,
                    price,
                    qty,
                    _entry_fill_timestamp(order),
                )
            except Exception as exc:
                log.warning(
                    "  Entry fill could not be recorded for trade %s: %s",
                    trade_id,
                    exc,
                )
            return
        if attempt < attempts - 1:
            time.sleep(delay)


def _entry_fill_timestamp(order) -> str | None:
    stamp = _parse_timestamp(getattr(order, "filled_at", None))
    return stamp.isoformat() if stamp is not None and not pd.isna(stamp) else None


def _backfill_entry_fill(tc, trade: dict) -> None:
    """Compatibility helper using the same verified terminal-entry refresh."""
    try:
        _reconcile_entry_fill(tc, trade)
    except Exception as exc:
        log.warning("  %s entry fill backfill failed: %s", trade["ticker"], exc)


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


def _open_position_or_none(tc, ticker: str):
    """The account position for ``ticker``, or None only when Alpaca says none.

    Alpaca answers 404 for a symbol with no position. Anything else (429, 5xx,
    timeout, DNS) is unknown state and is re-raised: treating it as "the
    position is gone" would finalize a live trade from a network blip.
    """
    try:
        return tc.get_open_position(ticker)
    except Exception as exc:
        if _exception_status_code(exc) == 404:
            return None
        raise


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


# ── Alpaca client (lazy) ──────────────────────────────────────────────────────

_trading_client = None
def _get_trading():
    global _trading_client
    if _trading_client is None:
        if not ALPACA_PAPER:
            log.warning("⚠️  ALPACA_PAPER=false — would trade REAL MONEY! Forcing paper=True")
        from alpaca.trading.client import TradingClient
        # PAPER ONLY — hardcoded paper=True as safety override
        from http_timeouts import apply_default_timeout
        _trading_client = apply_default_timeout(
            TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)
        )
    return _trading_client


# ── Data fetch ────────────────────────────────────────────────────────────────

def fetch_bars(
    ticker: str, days: int = 90, timeframe: str = BAR_TIMEFRAME
) -> pd.DataFrame:
    """Recent bars for live trading, selected by strategy timeframe."""
    return data_feed.fetch_recent(ticker, days=days + 30, timeframe=timeframe)


# ── Signal timing (live/backtest parity) ──────────────────────────────────────
#
# The backtest turns every completed signal bar into at most ONE candidate,
# filled at the first regular-session minute at/after the bar completes. Live
# mirrors that: every bar completed since the last evaluated one is examined
# once (so close-of-day and after-hours bars are not skipped), and a signal is
# only actionable during the first loop interval after its backtest fill time
# (so a stale bar can never produce a late entry or a re-entry after an exit).

_loop_interval = timedelta(minutes=30)   # set by run_loop; 30 for one-shot runs
SIGNAL_FILL_GRACE = timedelta(minutes=5)


def _utc_naive(value) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_convert("UTC").tz_localize(None) if ts.tzinfo else ts


def _signal_fill_time(bar_start, timeframe: str) -> pd.Timestamp | None:
    """First regular-session minute at/after a bar completes (UTC, naive).

    Same rule as backtest_execution.collect_session_candidates: availability
    from signal_availability, then the first XNYS minute at or after it.
    """
    from backtest_execution import signal_availability
    from holding_period import calendar

    available = signal_availability(pd.DatetimeIndex([_utc_naive(bar_start)]), timeframe)[0]
    schedule = calendar(available.year, available.year + 1).schedule
    opens = pd.DatetimeIndex(schedule["open"]).tz_localize(None)
    closes = pd.DatetimeIndex(schedule["close"]).tz_localize(None)
    session = closes.searchsorted(available, side="right")
    if session >= len(closes):
        return None
    return max(available, opens[session])


def _signal_is_actionable(bar_start, timeframe: str, now=None) -> bool:
    """True only within one loop interval (+grace) of the modelled fill time."""
    try:
        fill = _signal_fill_time(bar_start, timeframe)
    except Exception as exc:
        log.warning("  Signal fill time unavailable for bar %s (%s)", bar_start, exc)
        return False
    if fill is None:
        return False
    current = _utc_naive(now if now is not None else datetime.now(timezone.utc))
    return fill <= current < fill + _loop_interval + SIGNAL_FILL_GRACE


# A pass is scheduled this long after each modelled fill time: after the 4h
# bucket has settled (data_feed.BAR_SETTLE) and well inside the fill window.
FILL_WAKE_DELAY = timedelta(seconds=90)


def _next_signal_fill_after(now, timeframe: str) -> pd.Timestamp | None:
    """Earliest modelled fill time strictly after ``now`` (UTC, naive).

    Candidate bars span a couple of days either side of now, which always
    includes the next session open (weekends and holidays included).
    """
    current = _utc_naive(now)
    if timeframe == "4h":
        base = current.floor("4h")
        starts = [base + pd.Timedelta(hours=4 * k) for k in range(-2, 13)]
    elif timeframe == "1d":
        # Any instant inside the New York day identifies that day's bar.
        noon = (current.tz_localize("UTC").tz_convert(_ET).normalize()
                + pd.Timedelta(hours=12))
        starts = [noon + pd.DateOffset(days=k) for k in range(-2, 5)]
    else:
        return None
    fills = [fill for fill in (_signal_fill_time(start, timeframe) for start in starts)
             if fill is not None and fill > current]
    return min(fills) if fills else None


def _seconds_until_next_pass(now, interval: timedelta, timeframe: str) -> float:
    """Sleep one interval, or less so a pass lands just after the next fill time.

    The backtest fills at the first session minute after a bar completes; a
    fixed sleep put live passes anywhere up to one interval later. Waking at
    each fill time keeps live entries within ~FILL_WAKE_DELAY of the model.
    """
    current = _utc_naive(now)
    wake = current + interval
    try:
        fill = _next_signal_fill_after(current, timeframe)
    except Exception as exc:
        log.warning("Next fill time unavailable (%s) — sleeping one interval", exc)
        fill = None
    if fill is not None and fill + FILL_WAKE_DELAY < wake:
        wake = fill + FILL_WAKE_DELAY
    return max(1.0, (wake - current).total_seconds())


def _modelled_fill_iso(bar_start, timeframe: str) -> str | None:
    """The backtest's fill time for this signal bar; None if unknowable."""
    try:
        fill = _signal_fill_time(bar_start, timeframe)
    except Exception:
        return None
    return fill.isoformat() if fill is not None else None


# The backtest takes the first minute WITH A TRADE at/after the modelled time;
# look this far ahead for it.
MODEL_FILL_SEARCH = timedelta(minutes=15)


def _record_execution_quality(now=None) -> int:
    """Store each live entry's modelled fill time and that minute's open.

    Bookkeeping only (never raises). ``entry_filled_at - modelled_fill_at``
    is the live fill delay and ``entry_filled_price / model_fill_price - 1``
    the live slippage against the backtest's own execution assumption.
    Returns how many trades were priced.
    """
    now = _utc_naive(now if now is not None else datetime.now(timezone.utc))
    priced = 0
    try:
        for trade in db_mod.get_trades_missing_modelled_fill():
            strat_obj = REGISTRY.get(str(trade.get("strategy") or ""))
            fill = _modelled_fill_iso(trade.get("entry_date"), strat_obj.timeframe) if strat_obj else None
            if fill is not None:
                db_mod.set_modelled_fill(trade["id"], fill)
        since = (now - timedelta(days=30)).isoformat()
        for trade in db_mod.get_trades_missing_model_price(since):
            start = _utc_naive(trade["modelled_fill_at"])
            if start + MODEL_FILL_SEARCH > now:
                continue  # the search window has not fully printed yet
            begin = start.tz_localize("UTC").to_pydatetime()
            bars = data_feed.fetch_bars(trade["ticker"], begin, begin + MODEL_FILL_SEARCH,
                                        timeframe="1min")
            if bars is None or bars.empty:
                continue
            bars = bars[bars.index >= start]
            if not bars.empty:
                db_mod.set_model_fill_price(trade["id"], float(bars["open"].iloc[0]))
                priced += 1
    except Exception as exc:
        log.warning("Execution quality not recorded (%s)", exc)
    return priced


def _bar_key(bar_start) -> str:
    return _utc_naive(bar_start).isoformat()


def _unevaluated_bar_indexes(strat_name: str, ticker: str, df: pd.DataFrame) -> list[int]:
    """Completed bars newer than the stored cursor, oldest first.

    With no cursor yet (first run / new ticker) only the latest bar is new, so
    a restart never replays history.
    """
    cursor = db_mod.get_signal_cursor(strat_name, ticker)
    if cursor is None:
        return [len(df) - 1]
    after = pd.Timestamp(cursor)
    return [i for i, stamp in enumerate(df.index) if _utc_naive(stamp) > after]


def _signal_exit_reason(strat_obj, frame: pd.DataFrame, trade: dict) -> str | None:
    """Oldest exit signal on any completed bar after the entry's signal bar.

    The backtest latches an exit signal from any bar after the signal bar, so a
    cross on a bar that completed while the market was closed (and is no longer
    the latest bar by the next session) still exits.
    """
    last = len(frame) - 1
    try:
        signal_bar = _utc_naive(trade.get("entry_date"))
        indexes = [i for i, stamp in enumerate(frame.index) if _utc_naive(stamp) > signal_bar]
    except (TypeError, ValueError):
        indexes = [last]
    for idx in indexes or []:
        reason = strat_obj.check_exit(frame, idx, PARAMS)
        if reason:
            return reason
    return None


# ── Main trading loop ─────────────────────────────────────────────────────────

def run_once(strategy: StrategyType) -> int:
    """Single pass: check each ticker, enter if signal, check open positions."""
    strat_name = strategy.value
    run_id = db_mod.start_bot_run(strat_name)
    log.info("=" * 50)
    log.info("Bot V2 run — strategy=%s", strat_name)
    log.info("=" * 50)

    orders_placed = 0
    cycle_started = time.monotonic()
    trades_found = 0
    errors: list[str] = []
    tc = None

    def record_error(phase: str, exc: Exception) -> None:
        message = f"{phase}: {exc}"
        errors.append(message)
        log.error("Bot run failed — %s", message)

    frames: dict[str, pd.DataFrame] = {}

    try:
        strat_obj = REGISTRY[strat_name]
        tc = _get_trading()
        entry_capacity_blocked = _entry_capacity_is_unsettled(tc)
        sizing_state = None if entry_capacity_blocked else _load_live_sizing(tc)

        # A strategy scoped to its own instruments (e.g. tqqq_momentum) trades
        # only those; everything else trades the shared universe.
        for ticker in strategy_universe(strat_obj, TICKERS):
            entry_db_id = None
            try:
                log.info("Checking %s...", ticker)
                df = fetch_bars(
                    ticker, days=PARAMS.history_days, timeframe=strat_obj.timeframe
                )
                df = data_feed.completed_bars(df, strat_obj.timeframe)
                if df.empty or len(df) < 60:
                    log.warning("%s: insufficient data (got %d bars)", ticker, len(df))
                    continue

                df = add_indicators(df, PARAMS)
                if strat_name in SKIP_EARNINGS_STRATEGIES:
                    try:
                        df = add_earnings_filter(df, ticker, PARAMS, live=True)
                    except Exception as exc:
                        # Calendar/storage failures must suppress this signal,
                        # while leaving other strategies and exit reconciliation running.
                        log.warning('%s: earnings policy unavailable (%s)', ticker, type(exc).__name__)
                        df['near_earnings'] = True
                        df['earnings_status'] = 'unknown'
                frames[ticker] = df

                # Examine every newly completed bar once; only a bar still
                # inside its modelled fill window may produce an entry.
                sig = None
                for idx in _unevaluated_bar_indexes(strat_name, ticker, df):
                    candidate = strat_obj.check_entry(df, idx, PARAMS)
                    if candidate is None:
                        continue
                    if not _signal_is_actionable(df.index[idx], strat_obj.timeframe):
                        log.info("  %s: signal on bar %s is outside its fill window — skipped",
                                 ticker, df.index[idx])
                        continue
                    sig = candidate
                    break
                # Advance before any order work: a bar is never acted on twice,
                # and a cursor that cannot be stored blocks the entry.
                db_mod.set_signal_cursor(strat_name, ticker, _bar_key(df.index[-1]))
                # A signal on a ticker this bot already holds can never become an
                # order, so it is not counted or stored as a signal.
                if sig is not None and db_mod.get_open_trade(ticker, strat_name):
                    log.info("  %s: signal while already holding an open trade — skipped",
                             ticker)
                    continue
                if sig is not None:
                    trades_found += 1
                    if strat_obj.has_take_profit:
                        log.info("  SIGNAL: %s entry at $%.2f (SL $%.2f / TP $%.2f)",
                                 ticker, sig.entry_price, sig.stop_loss, sig.take_profit)
                    else:
                        log.info("  SIGNAL: %s entry at $%.2f (cross exit / emergency stop)",
                                 ticker, sig.entry_price)
                    bot_hooks.log_signal(sig, ticker, strat_name)

                    # Only enter if TP1 is within 4 ATRs of the entry. The ATR is on
                    # the strategy's own candles, so on 4h bars this is 4 bar-ATRs.
                    if (strat_obj.has_take_profit and
                            not is_tp_reachable_in_days(sig.entry_price, sig.tp1, sig.atr, days=4)):
                        log.info("  TP1 $%.2f not reachable (ATR=%.2f) — skipping",
                                 sig.tp1, sig.atr)
                        continue

                    # Year-end wash-sale guard. Only active from December: an
                    # intra-year wash sale defers a loss into the replacement lot's
                    # basis and is recovered on the next sale, but one still open on
                    # 31 December pushes the deduction into the next tax year.
                    tax_block = _tax_entry_block(ticker)
                    if tax_block:
                        log.info("  %s", tax_block)
                        continue

                    # Also avoid stacking on top of any pre-existing position (e.g. one a
                    # human opened). Only a definitive 404 proves "no position"; any
                    # other failure (network, outage) is unknown state, so skip the
                    # entry rather than risk doubling exposure.
                    try:
                        open_pos = _open_position_or_none(tc, ticker)
                    except Exception as pos_exc:
                        log.warning(
                            "  %s: position lookup failed (%s) — skipping entry",
                            ticker,
                            pos_exc,
                        )
                        continue
                    if open_pos is not None:
                        log.info("  A %s position already exists (qty %s) not tracked by the bot — skipping",
                                 ticker, open_pos.qty)
                        continue

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

                        # The SL/TP geometry was computed off the signal bar close.
                        # If the live price has already drifted away, that geometry
                        # no longer matches the backtest — skip rather than chase.
                        if strat_obj.has_take_profit and sig.entry_price > 0:
                            slippage = abs(market_ref - sig.entry_price) / sig.entry_price
                            if slippage > PARAMS.entry_max_slippage_pct:
                                log.info(
                                    "  %s: live $%.2f is %.2f%% from signal $%.2f "
                                    "(max %.2f%%) — skipping entry",
                                    ticker,
                                    market_ref,
                                    slippage * 100,
                                    sig.entry_price,
                                    PARAMS.entry_max_slippage_pct * 100,
                                )
                                continue

                        is_leveraged = ticker in set(LEVERAGED_TICKERS)
                        headroom = combine_headroom(
                            leveraged_headroom(
                                sizing_state.equity,
                                sizing_state.leveraged_notional,
                                PARAMS.max_leveraged_exposure_pct,
                            )
                            if is_leveraged
                            else None,
                            group_headroom(
                                sizing_state.equity,
                                ticker,
                                sizing_state.open_notional_by_ticker,
                                PARAMS.exposure_groups,
                            ),
                        )
                        size = whole_share_position_size(
                            sizing_state.equity,
                            sizing_state.remaining_cash,
                            market_ref,
                            PARAMS.position_size_pct,
                            max_notional=headroom,
                        )
                        qty = size.quantity
                        if qty < 1:
                            if size.reason == "group_exposure_cap":
                                log.info(
                                    "  %s: entry skipped — exposure cap (leveraged "
                                    "or correlated group) leaves $%.2f headroom, "
                                    "below one share @ $%.2f",
                                    ticker,
                                    headroom,
                                    market_ref,
                                )
                                continue
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
                            # Each signal strategy owns its emergency-stop
                            # distance (TQQQ 8%, SMA cross 10%), exactly as the
                            # backtest applies it.
                            sig.stop_loss = market_ref * (
                                1.0 - strat_obj.stop_loss_fraction(PARAMS)
                            )

                        entry_coid = _make_client_order_id(
                            strat_name, ticker, "entry"
                        )
                        modelled_fill = _modelled_fill_iso(sig.date, strat_obj.timeframe)
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
                            modelled_fill_at=modelled_fill,
                        )

                        def record_accepted_entry(accepted_info: dict) -> None:
                            nonlocal entry_accepted
                            entry_accepted = True
                            sizing_state.remaining_cash = max(
                                0.0, sizing_state.remaining_cash - size.notional
                            )
                            sizing_state.remaining_slots -= 1
                            if is_leveraged:
                                # Reserve within this cycle too: correlated ETFs
                                # signal together, so later tickers in the same
                                # pass must see this entry's exposure.
                                sizing_state.leveraged_notional += size.notional
                            # Same for correlated-group caps: four names entered
                            # on one bar within 14 s on 2026-09-21.
                            sizing_state.open_notional_by_ticker[ticker] = (
                                sizing_state.open_notional_by_ticker.get(ticker, 0.0)
                                + size.notional
                            )
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
                            coid = info["entry_coid"]
                            log.info("  Stop-only OTO: %s x%d (SL $%.2f) [coid=%s]",
                                     ticker, qty, sig.stop_loss, coid)
                        else:
                            # One protected bracket (TP3 + SL) per entry, whatever the
                            # quantity. Alpaca rejects extra concurrent sell legs
                            # (403 40310000), and the single bracket also backtested
                            # better than the 3-leg scale-out across 2024-2026.
                            info = _place_single_bracket_entry(
                                tc,
                                ticker,
                                qty,
                                sig,
                                strat_name,
                                entry_coid=entry_coid,
                            )
                            record_accepted_entry(info)
                            coid = info["entry_coid"]
                            log.info("  Bracket entry: %s x%d @ ~$%.2f (SL $%.2f / TP $%.2f) [coid=%s]",
                                     ticker, qty, market_ref, sig.stop_loss, sig.tp3, coid)

                        orders_placed += 1
                        _record_entry_fill(tc, entry_db_id, info.get("alpaca_id"))
                        if strat_obj.has_take_profit:
                            body = (f"Entry ~${market_ref:.2f} (signal ${sig.entry_price:.2f})\n"
                                    f"SL ${sig.stop_loss:.2f}\nTP ${sig.tp3:.2f}\n"
                                    f"Qty {qty}\nRef {coid}")
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
            except Exception as exc:
                # A failed/mutated frame is not evidence for a signal exit.
                frames.pop(ticker, None)
                if entry_db_id is not None:
                    # An escaped submission/recovery failure may have consumed
                    # cash or a slot. Reconciliation owns recovery; do not buy
                    # again using this cycle's potentially stale capacity.
                    sizing_state = None
                record_error(f"{ticker} entry scan", exc)

    except Exception as exc:
        record_error("Entry setup/scan", exc)

    # Entry data and submission errors must never starve existing holdings.
    # This phase retains its own per-trade ownership and idempotency guards.
    # Per-trade failures are alerted once per day by the reconciler itself; they
    # mark the run as errored but are not re-emailed every cycle below.
    alerted: list[str] = []
    try:
        alerted.extend(_reconcile_and_exit(strat_name, frames) or [])
    except Exception as exc:
        record_error("Reconciliation", exc)
    try:
        alerted.extend(_audit_protection())
    except Exception as exc:
        record_error("Protection audit", exc)

    try:
        # Tax records are recomputed over the whole history because a new
        # purchase can retroactively wash an earlier loss.
        _refresh_tax_records()
        _record_execution_quality()

        # Confirm our own positions with the broker and append a point to the
        # bot's local equity curve. Bookkeeping only — never places an order.
        if tc is not None:
            _record_balance_snapshot(strat_name, tc)

    except Exception as exc:
        record_error("Bookkeeping", exc)

    # Report only after risk management has had its opportunity to run.
    if errors:
        try:
            send_notification("Bot V2 Error", "\n".join(errors) + f"\n\nRun: {datetime.now().isoformat()}")
        except Exception as exc:
            record_error("Error notification", exc)
    error = "\n".join(errors + alerted) or None
    run_status = "error" if error else "done"
    db_mod.finish_bot_run(run_id, trades_found, orders_placed, error)
    log.info("Bot V2 run complete: %d signals, %d orders — status=%s (%.1fs)",
             trades_found, orders_placed, run_status, time.monotonic() - cycle_started)
    return 0 if not error else 1


def _max_hold_days(strategy: str) -> int:
    """Per-strategy time-stop horizon in exchange session closes."""
    from holding_period import holding_sessions_limit
    return holding_sessions_limit(strategy, PARAMS)


def _time_stop_due(trade: dict, *, as_of=None) -> bool:
    """Unknown broker fill time cannot authorize a time-based sell."""
    from holding_period import holding_deadline, utc
    try:
        fill = utc(trade.get("entry_filled_at"))
        now = utc(datetime.now(timezone.utc) if as_of is None else as_of)
        if fill > now:
            return False
        return now >= holding_deadline(fill, _max_hold_days(str(trade.get("strategy") or "")))
    except (ValueError, TypeError, OverflowError):
        return False


def _days_held(entry_date: str) -> int:
    try:
        return (date.today() - pd.to_datetime(entry_date).date()).days
    except Exception:
        return 0


def _hold_days_since_entry(trade: dict) -> float:
    """Elapsed-day reporting only; time-stop eligibility uses exchange sessions.

    Legacy records may fall back to creation/signal time for this diagnostic,
    never for an exit decision.
    """
    entry_ts = (_parse_timestamp(trade.get("entry_filled_at"))
                or _parse_timestamp(trade.get("created_at"))
                or _parse_timestamp(trade.get("entry_date")))
    if entry_ts is None:
        return 0.0
    return (datetime.now(timezone.utc) - entry_ts).total_seconds() / 86400.0


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


def _signal_exit_frame(
    ticker: str,
    strat_obj,
    cache: dict[tuple[str, str], pd.DataFrame],
) -> pd.DataFrame:
    """Fetch (once per cycle) a completed, indicator-ready frame for an exit check."""
    key = (ticker, strat_obj.timeframe)
    if key not in cache:
        try:
            df = fetch_bars(
                ticker, days=PARAMS.history_days, timeframe=strat_obj.timeframe
            )
            df = data_feed.completed_bars(df, strat_obj.timeframe)
            cache[key] = add_indicators(df, PARAMS) if not df.empty else df
        except Exception as e:
            log.warning("  %s: exit frame fetch failed: %s", ticker, e)
            cache[key] = pd.DataFrame()
    return cache[key]


def _reconcile_and_exit(
    strat_name: str, frames: dict[str, pd.DataFrame] | None = None
) -> list[str]:
    """Keep DB trades in sync and apply each trade's own strategy exit.

    Covers EVERY open trade this bot recorded — whatever strategy opened it —
    so restarting the singleton with a different strategy never orphans the
    older strategy's positions. For each open trade:
      * if its Alpaca position is gone (a bracket SL/TP filled) → record the exit.
      * else if past max-hold and at breakeven+ → close OUR quantity (after verifying
        ownership and cancelling our own bracket legs).
    Positions the bot did not open are never inspected or closed.

    Returns one message per trade whose reconciliation failed. Each failure is
    also alerted (once per trade per day): fail-closed states such as an
    unresolved protective submission need an operator and must not stay silent.
    """
    failures: list[str] = []
    try:
        tc = _get_trading()
    except Exception as e:
        log.debug("Exit check skipped (no trading client): %s", e)
        return failures

    try:
        open_trades = db_mod.get_open_trades()
    except Exception as e:
        log.error("Exit check skipped: open trades unavailable (%s)", e)
        return [f"open trades unavailable: {e}"]

    frame_cache: dict[tuple[str, str], pd.DataFrame] = {}
    for trade in open_trades:
        ticker = trade["ticker"]
        trade_strat = str(trade.get("strategy") or "")
        strat_obj = REGISTRY.get(trade_strat)
        if strat_obj is None:
            log.warning(
                "  %s: unknown strategy %r — leaving trade untouched",
                ticker,
                trade_strat,
            )
            continue
        try:
            if not _resolve_pending_entry(tc, trade):
                continue
            # Every exit path needs actual terminal fills, including pending
            # exits and signal-driven positions already absent at the broker.
            # Unknown/unsettled ownership must not finalize or initiate a sell.
            if _reconcile_entry_fill(tc, trade) is None:
                continue
            # Only a 404 proves the position is gone; any other lookup failure
            # raises into this trade's failure handler and fails closed.
            pos = _open_position_or_none(tc, ticker)

            # A previously submitted market exit owns this trade until Alpaca
            # confirms that it filled or reached a terminal unfilled state.
            # Never place a second sell while the first is still live/unknown.
            if trade.get("exit_alpaca_order_id") and _reconcile_pending_exit(tc, trade):
                continue

            # Account-wide symbol quantity does not prove that OUR trade is
            # still open. Its bracket may have filled while another owner holds
            # the same symbol. Reconcile exact linked orders first, even when
            # the account still has a position (or a durable exit intent).
            if strat_obj.exit_mode == "bracket":
                linked = _owned_bracket_orders(tc, trade)
                if _record_bracket_progress(trade, linked):
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
                linked = _owned_bracket_orders(tc, trade)
                if _record_bracket_progress(trade, linked):
                    continue
                frame = (frames or {}).get(ticker) if trade_strat == strat_name else None
                if frame is None or frame.empty:
                    frame = _signal_exit_frame(ticker, strat_obj, frame_cache)
                if frame is None or frame.empty:
                    log.warning("  %s: no completed daily frame — leaving position open", ticker)
                    _ensure_owned_protection(tc, trade)
                    continue
                if not _verify_owned(tc, trade):
                    log.warning("  %s cross exit blocked: ownership unverified (coid=%s)",
                                ticker, trade.get("client_order_id"))
                    continue
                reason = _signal_exit_reason(strat_obj, frame, trade)
                if reason:
                    _close_owned(tc, trade, pos, reason=reason)
                else:
                    _ensure_owned_protection(tc, trade)
                continue

            current = float(pos.current_price)
            entry = _effective_entry_price(trade)
            if _time_stop_due(trade) and current >= entry:
                if not _verify_owned(tc, trade):
                    log.warning("  %s past max-hold but ownership unverified (coid=%s) — leaving it alone",
                                ticker, trade.get("client_order_id"))
                    continue
                _close_owned(tc, trade, pos, reason="time_stop")
            else:
                _ensure_owned_protection(tc, trade)
        except EntryPending as pending:
            if _entry_pending_is_young(pending):
                log.info("  %s: entry order still working — reconciling next cycle", ticker)
                continue
            _record_reconcile_failure(failures, trade, pending)
        except Exception as e:
            _record_reconcile_failure(failures, trade, e)
    return failures


def _entry_pending_is_young(pending: "EntryPending", now=None) -> bool:
    """A working entry inside the grace window is normal, not a failure."""
    submitted = pending.submitted_at
    if submitted is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now - submitted < ENTRY_PENDING_GRACE


def _record_reconcile_failure(failures: list[str], trade: dict, e: Exception) -> None:
    """Record one trade's reconciliation failure and alert once per day."""
    ticker = trade["ticker"]
    log.error("  Exit check failed for %s: %s", ticker, e)
    message = f"{ticker} trade {trade.get('id')}: {e}"
    failures.append(message)
    _alert_once(
        f"reconcile-{trade.get('id')}",
        f"Bot V2: {ticker} reconciliation blocked",
        f"{message}\n\nThe bot fails closed and will not place orders for this "
        f"trade until the broker state is resolved. Check that the position "
        f"still has a live stop.",
    )


_ALERT_MARKER = ROOT / "run" / "alerts.json"


def _alert_once(key: str, subject: str, body: str) -> bool:
    """Email at most once per key per ET trading day; always logs."""
    log.error("ALERT %s: %s", subject, body.splitlines()[0] if body else "")
    today = datetime.now(_ET).date().isoformat()
    try:
        sent = json.loads(_ALERT_MARKER.read_text()) if _ALERT_MARKER.exists() else {}
        if not isinstance(sent, dict):
            sent = {}
    except Exception:
        sent = {}
    if sent.get(key) == today:
        return False
    sent = {k: v for k, v in sent.items() if v == today}
    sent[key] = today
    try:
        _ALERT_MARKER.parent.mkdir(exist_ok=True)
        _ALERT_MARKER.write_text(json.dumps(sent))
    except Exception:
        pass  # a marker failure must never suppress the alert itself
    send_notification(subject, body)
    return True


_ACTIVE_ORDER_STATES = {"new", "accepted", "held", "pending_new", "partially_filled", "done_for_day"}


def _protection_gap(tc, trade: dict) -> str | None:
    """Why a filled, owned position has no live broker stop, or None.

    Only evidence of an owned, settled position counts: unsettled entries, a
    missing broker position and a working controlled exit are not gaps.
    """
    if trade.get("entry_state") == "pending_submission":
        return None
    try:
        if _reconcile_entry_fill(tc, trade) is None:
            return None
    except Exception:
        return None  # entry unsettled/unknown; reconciliation reports it
    try:
        pos = tc.get_open_position(trade["ticker"])
    except Exception:
        return None
    if pos is None or float(getattr(pos, "qty", 0) or 0) <= 0:
        return None
    if float(trade.get("shares") or 0) - db_mod.get_exit_fill_totals(trade["id"])[0] < 1e-9:
        return None
    exit_id = trade.get("exit_alpaca_order_id")
    if exit_id:
        try:
            if _status_str(tc.get_order_by_id(exit_id)) in _ACTIVE_ORDER_STATES:
                return None  # a market exit is working; protection was removed on purpose
        except Exception:
            pass
    try:
        orders = _owned_bracket_orders(tc, trade)
    except Exception as exc:
        return f"protection cannot be verified ({exc})"
    if orders is None:
        return None
    for order in orders:
        kind = str(getattr(getattr(order, "type", ""), "value", getattr(order, "type", "")))
        if kind == "stop" and _status_str(order) in _ACTIVE_ORDER_STATES:
            return None
    return "no live stop order is linked to this trade"


def _audit_protection() -> list[str]:
    """Alert (once per trade per day) on any owned position without a live stop."""
    gaps: list[str] = []
    try:
        tc = _get_trading()
        trades = db_mod.get_open_trades()
    except Exception as exc:
        log.warning("Protection audit skipped (%s)", exc)
        return gaps
    for trade in trades:
        try:
            reason = _protection_gap(tc, trade)
        except Exception as exc:
            reason = f"protection audit failed ({exc})"
        if reason is None:
            continue
        message = f"{trade['ticker']} trade {trade.get('id')}: {reason}"
        gaps.append(message)
        _alert_once(
            f"unprotected-{trade.get('id')}",
            f"Bot V2: {trade['ticker']} position is UNPROTECTED",
            f"{message}\n\nThe broker holds this bot's shares with no active stop. "
            f"The bot keeps retrying repairs; if this persists, place or verify "
            f"a stop manually.",
        )
    return gaps


def _reconcile_closed(tc, trade: dict):
    """Our tracked position is gone — either a bracket leg filled, or the entry
    order itself never filled and was later canceled/expired by the broker."""
    ticker = trade["ticker"]
    entry = _effective_entry_price(trade)

    if _entry_never_filled(tc, trade):
        db_mod.close_trade(trade["id"], datetime.now(timezone.utc).isoformat(), entry,
                           "entry_not_filled", _days_held(trade["entry_date"]), 0.0, 0.0, 0.0,
                           exit_client_order_id=trade.get("client_order_id"),
                           exit_alpaca_order_id=trade.get("alpaca_order_id"))
        log.info("  Reconciled: %s entry order never filled for trade %s (coid=%s) — closing, no position taken",
                 ticker, trade.get("id"), trade.get("client_order_id"))
        return True

    exit_fill = _confirmed_exit_fill(tc, trade)
    if exit_fill is not None:
        return _record_confirmed_exit(
            trade, exit_fill, _exit_reason_for_fill(trade, exit_fill)
        )

    # Our position is gone and none of our own orders closed it. Before giving
    # up, check whether a sibling project on the shared key liquidated us —
    # otherwise the row stays open forever and silently burns a position slot.
    foreign = _foreign_liquidation_fill(tc, trade)
    if foreign is not None:
        log.error(
            "  %s trade %s was liquidated by a foreign order (%s) x%g @ $%.2f — "
            "closing locally as external_liquidation",
            ticker, trade.get("id"), foreign.get("alpaca_order_id"),
            foreign["shares"], foreign["price"],
        )
        send_notification(
            f"Bot V2: {ticker} liquidated externally",
            f"{ticker} x{foreign['shares']:g} was sold @ ${foreign['price']:.2f} by order "
            f"{foreign.get('alpaca_order_id')}, which this bot never placed.\n\n"
            f"Another project sharing this Alpaca key almost certainly ran an "
            f"account-wide close_all_positions(). The trade has been closed locally "
            f"so its position slot is released.\n\n"
            f"Durable fix: give this bot its own Alpaca account/key.",
        )
        return _record_confirmed_exit(trade, foreign, "external_liquidation")

    log.warning("  %s position missing but no confirmed exit fill for trade %s (coid=%s) — leaving open",
                ticker, trade.get("id"), trade.get("client_order_id"))
    return False


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
    # Most precise link first: protection re-armed after entry is a standalone
    # order, so it is unreachable via the entry's legs below. Its stored id (and
    # the legs it owns) proves the fill is ours without any heuristic.
    for order in _protect_order_candidates(tc, trade):
        for candidate in (*(getattr(order, "legs", None) or []), order):
            fill = _exit_fill_from_order(candidate, require_prefix=False)
            if fill and not db_mod.exit_order_already_used(fill["alpaca_order_id"]):
                return fill

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


def _foreign_sell_orders(tc, ticker: str) -> list:
    """Filled SELL orders for the symbol that this bot did NOT place.

    The account is shared with sibling projects, so a sell we never submitted is
    a normal (if unwelcome) event rather than a data error. Kept separate from
    `_our_sell_orders` on purpose: ownership still gates every exit this bot
    *initiates*, and only the post-mortem below may look at foreign fills.
    """
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus, OrderSide

    out = []
    try:
        req = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED, symbols=[ticker], side=OrderSide.SELL, limit=100
        )
        out.extend(tc.get_orders(filter=req) or [])
    except Exception as e:
        log.debug("  %s: foreign sell lookup failed: %s", ticker, e)
    return [
        o
        for o in out
        if not str(getattr(o, "client_order_id", "") or "").startswith(CLIENT_ORDER_PREFIX)
    ]


def _foreign_liquidation_fill(tc, trade: dict) -> dict | None:
    """A sell this bot never placed that closed out our position.

    Sibling projects on the same Alpaca key run account-wide
    `close_all_positions()`, which liquidates this bot's shares through orders
    carrying someone else's client id. Attribution is still evidence-based — the
    fill must be a real filled sell of at least our quantity, recorded after our
    entry, and unclaimed by any other DB trade. P&L is credited for our share
    count only, so an aggregated flatten covering several bots cannot inflate it.
    """
    owned = float(trade.get("shares") or 0)
    if owned <= 0:
        return None

    # Fail closed on unreadable broker state. Our own bracket legs carry
    # broker-generated client ids, so they look "foreign" by prefix alone and are
    # ruled out only by reaching them through the parent order above. If that
    # parent could not be read, a legitimate stop fill is indistinguishable from
    # someone else's flatten — leave the trade open rather than mislabel it.
    if not _entry_order_candidates(tc, trade) and not _protect_order_candidates(tc, trade):
        log.warning(
            "  %s: cannot read our own orders — not attributing the missing "
            "position to a foreign sell",
            trade["ticker"],
        )
        return None

    candidates = []
    for order in _foreign_sell_orders(tc, trade["ticker"]):
        if not _order_is_after_trade(order, trade):
            continue
        fill = _exit_fill_from_order(order, require_prefix=False)
        if fill is None or fill["shares"] < owned - 1e-9:
            continue
        if db_mod.exit_order_already_used(fill["alpaca_order_id"]):
            continue
        ts = _parse_timestamp(getattr(order, "filled_at", None)) or _parse_timestamp(
            getattr(order, "submitted_at", None)
        )
        # Credit our shares at their price; the surplus belongs to another bot.
        fill["shares"] = owned
        fill["notional"] = owned * fill["price"]
        candidates.append((ts, fill))

    if not candidates:
        return None
    # Earliest qualifying fill is the one that actually took our position away.
    candidates.sort(key=lambda pair: (pair[0] is None, pair[0]))
    return candidates[0][1]


def _protect_order_candidates(tc, trade: dict) -> list:
    """The re-armed protective order for this trade, resolved by its stored ids."""
    from alpaca.trading.requests import GetOrderByIdRequest

    out = []
    order_id = trade.get("protect_alpaca_order_id")
    if order_id:
        try:
            # nested=True or Alpaca omits the OCO child legs entirely.
            out.append(
                tc.get_order_by_id(order_id, filter=GetOrderByIdRequest(nested=True))
            )
        except Exception:
            pass

    coid = trade.get("protect_client_order_id")
    if coid:
        try:
            out.append(tc.get_order_by_client_id(coid))
        except Exception:
            pass
    return [o for o in out if o is not None]


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
    # A re-armed OCO fills through one of its own child legs, so match the parent
    # id too — the leg carries a broker-generated client id, not ours.
    if "-protect-" in coid or (
        trade.get("protect_alpaca_order_id")
        and fill.get("alpaca_order_id") == trade.get("protect_alpaca_order_id")
    ):
        return "protective_bracket_filled"
    if "-exit-" in coid:
        return (
            strat_obj.signal_exit_reason
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
    entry = _effective_entry_price(trade)
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
            round(_hold_days_since_entry(trade)),
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


def _reconcile_entry_fill(tc, trade: dict):
    """Refresh actual owned quantity/basis independently of protection children.

    Return the verified terminal parent, or None after retiring a zero-fill
    rejection. Unsettled/unverifiable entries raise and leave exit intent intact.
    """
    from alpaca.trading.requests import GetOrderByIdRequest

    oid, coid = trade.get("alpaca_order_id"), trade.get("client_order_id")
    if not oid or not coid or not str(coid).startswith(CLIENT_ORDER_PREFIX + "-"):
        raise ValueError("missing owned entry references")
    entry = tc.get_order_by_id(oid, filter=GetOrderByIdRequest(nested=True))
    side = getattr(getattr(entry, "side", None), "value", getattr(entry, "side", None))
    if (str(getattr(entry, "id", "")) != str(oid)
            or getattr(entry, "client_order_id", None) != coid
            or getattr(entry, "symbol", None) != trade["ticker"] or side != "buy"):
        raise ValueError("broker entry does not match stored ownership")
    if _status_str(entry) not in {"filled", "closed", "canceled", "expired", "rejected"}:
        if _status_str(entry) in _WORKING_ENTRY_STATES:
            raise EntryPending(
                "entry quantity is still unsettled",
                _parse_timestamp(getattr(entry, "submitted_at", None))
                or _parse_timestamp(trade.get("created_at")),
            )
        raise ValueError("entry quantity is still unsettled")
    qty = float(entry.filled_qty)
    price = float(entry.filled_avg_price or 0)
    if qty == 0 and _status_str(entry) in {"canceled", "expired", "rejected"}:
        if (trade.get("exit_alpaca_order_id") or trade.get("exit_client_order_id")
                or trade.get("exit_intent_reason") or db_mod.get_exit_fill_totals(trade["id"])[0] > 0):
            raise ValueError("zero-filled entry conflicts with exit evidence; reconciliation required")
        db_mod.close_trade(
            trade["id"], datetime.now(timezone.utc).isoformat(),
            trade["entry_price"], "entry_not_filled", 0, 0.0, 0.0, 0.0,
            exit_client_order_id=trade["client_order_id"],
            exit_alpaca_order_id=trade["alpaca_order_id"],
        )
        return None
    if not (math.isfinite(qty) and qty > 0 and math.isfinite(price) and price > 0):
        raise ValueError("entry has no verified positive fill")
    filled_at = _entry_fill_timestamp(entry)
    db_mod.set_entry_fill(trade["id"], price, qty, filled_at)
    trade.update(shares=qty, entry_filled_price=price)
    if filled_at is not None:
        trade["entry_filled_at"] = filled_at

    return entry


def _owned_bracket_orders(tc, trade: dict) -> list | None:
    """Read exact stored parents and their children; never infer from a symbol.

    Raises on incomplete/unverified evidence so callers cannot initiate a sell.
    Child client ids are broker-generated: their parent link proves ownership.
    None means a verified terminal entry with zero fills was retired locally.
    """
    from alpaca.trading.requests import GetOrderByIdRequest

    def parent(id_key, coid_key, side):
        oid, coid = trade.get(id_key), trade.get(coid_key)
        if not oid and coid and id_key == "protect_alpaca_order_id":
            # Pre-submit intent survived a timeout/crash. Look up that exact
            # client id; 404/unknown is NOT permission to resubmit a sell.
            resolution, recovered = _lookup_entry_by_client_id(tc, coid)
            if resolution != "found":
                raise ValueError(f"protective submission unresolved ({resolution}); operator reconciliation required")
            oid = str(getattr(recovered, "id", "") or "")
        if not oid or not coid or not str(coid).startswith(CLIENT_ORDER_PREFIX + "-"):
            raise ValueError("missing owned order references")
        result = tc.get_order_by_id(oid, filter=GetOrderByIdRequest(nested=True))
        actual_side = getattr(result.side, "value", result.side)
        if (str(result.id) != str(oid) or result.client_order_id != coid
                or result.symbol != trade["ticker"] or actual_side != side):
            raise ValueError("broker order does not match stored ownership")
        if id_key == "protect_alpaca_order_id" and not trade.get(id_key):
            db_mod.set_protect_order_ids(trade["id"], coid, str(oid))
            trade[id_key] = str(oid)
        return result

    entry = _reconcile_entry_fill(tc, trade)
    if entry is None:
        return None

    linked = list(entry.legs or [])
    stop_only = REGISTRY[trade["strategy"]].exit_mode == "signal_with_stop"
    expected = 1 if stop_only else 2
    if len(linked) != expected or len({str(order.id) for order in linked}) != expected:
        raise ValueError("complete distinct entry protection children are required")
    if trade.get("protect_alpaca_order_id") or trade.get("protect_client_order_id"):
        protection = parent("protect_alpaca_order_id", "protect_client_order_id", "sell")
        if stop_only:
            if protection.legs:
                raise ValueError("replacement stop must be a standalone order")
        elif (len(protection.legs or []) != 1
              or str(protection.legs[0].id) == str(protection.id)):
            raise ValueError("complete replacement OCO parent and stop are required")
        linked.extend([protection, *(protection.legs or [])])
    unique = {}
    for child in linked:
        _validate_bracket_child(child, trade)
        unique[str(child.id)] = child
    return list(unique.values())


def _validate_bracket_child(order, trade: dict) -> None:
    side = getattr(order.side, "value", order.side)
    if not order.id or order.symbol != trade["ticker"] or side != "sell":
        raise ValueError("invalid linked sell order")
    qty = float(order.filled_qty)
    if not math.isfinite(qty) or qty < 0:
        raise ValueError("invalid linked fill quantity")
    if qty > 0:
        price = float(order.filled_avg_price or 0)
        if not math.isfinite(price) or price <= 0:
            raise ValueError("linked fill price is unavailable")


def _record_bracket_progress(trade: dict, orders: list | None) -> bool:
    """Persist cumulative partial/final fills once per Alpaca child order id."""
    if orders is None:
        return True
    reference = None
    for order in orders:
        progress = _account_order_progress(trade, order)
        if progress is not None:
            reference = progress
    reason = "stop_loss" if REGISTRY[trade["strategy"]].exit_mode == "signal_with_stop" else "bracket_filled"
    return _finalize_accumulated_exit(trade, reason, reference)


def _cancel_owned_bracket(tc, trade: dict) -> bool:
    """Confirm owned protection inactive and record racing fills before selling."""
    terminal = {"filled", "closed", "canceled", "expired", "rejected"}
    active = {"new", "accepted", "held", "pending_new", "partially_filled", "done_for_day"}
    try:
        orders = _owned_bracket_orders(tc, trade)
        if _record_bracket_progress(trade, orders):
            return False
        ids = {str(order.id) for order in orders}
        if any(_status_str(order) not in active | terminal | {"pending_cancel"} for order in orders):
            raise ValueError("unverified protection status; leaving all linked orders unchanged")
        for order in orders:
            status = _status_str(order)
            if status in active:
                try:
                    tc.cancel_order_by_id(order.id)
                except Exception as exc:
                    # Canceling one bracket/OCO leg cancels its sibling too.
                    # The stale second cancel may reject; only fresh terminal
                    # evidence below can authorize a replacement or market sell.
                    log.warning("  %s cancellation response for %s: %s; verifying state",
                                trade["ticker"], order.id, exc)
        for attempt in range(5):
            refreshed = [tc.get_order_by_id(oid) for oid in ids]
            if {str(order.id) for order in refreshed} != ids:
                raise ValueError("bracket lookup returned different order ids")
            for order in refreshed:
                _validate_bracket_child(order, trade)
            if _record_bracket_progress(trade, refreshed):
                return False
            if all(_status_str(order) in terminal for order in refreshed):
                return True
            if attempt < 4:
                time.sleep(0.2)
        raise ValueError("bracket cancellation is not confirmed")
    except Exception as exc:
        log.error("  %s exit blocked: owned bracket state unverified (%s)", trade["ticker"], exc)
        return False


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
    if not _cancel_owned_bracket(tc, trade):
        return False

    # Cancellation is asynchronous, so the position used to decide the exit may
    # now be stale. Refetch and subtract every durable partial fill before sizing.
    try:
        live_pos = _open_position_or_none(tc, ticker)
    except Exception as exc:
        # Unknown, not absent: keep the durable intent and retry next cycle.
        # The protection audit alerts while the position has no live stop.
        log.error("  %s exit deferred: position lookup failed (%s); intent retained",
                  ticker, exc)
        return False

    filled_shares, _filled_notional = db_mod.get_exit_fill_totals(trade["id"])
    our_qty = float(trade.get("shares") or 0)
    remaining_ours = max(0.0, our_qty - filled_shares) if our_qty > 0 else 0.0
    if live_pos is None:
        if _finalize_accumulated_exit(
            trade, reason, None
        ):
            return False
        _reconcile_closed(tc, trade)
        return False

    pos_qty = abs(float(live_pos.qty))
    qty_to_close = int(min(remaining_ours, pos_qty)) if our_qty > 0 else int(pos_qty)
    if qty_to_close < 1:
        if _finalize_accumulated_exit(
            trade, reason, None
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
_MARKET_OPEN  = dtime(9, 30)
_MARKET_CLOSE = dtime(16, 0)


# ── Local ledger ──────────────────────────────────────────────────────────────

def _broker_equity(tc) -> float | None:
    """Account equity, stored only as context.

    The key is shared with other projects, so this number moves on trades this
    bot never made. It is recorded beside the bot's own equity for comparison
    and is never an input to the bot's P&L.
    """
    try:
        return float(tc.get_account().equity)
    except Exception:
        return None


def _ledger_capital_base() -> float | None:
    """Return base for the ledger: the allocation, or None (peak deployed)."""
    allocation = PARAMS.bot_capital_allocation
    return allocation if math.isfinite(allocation) and allocation > 0 else None


def _ledger_capital_label() -> str:
    return "allocation   " if _ledger_capital_base() else "peak deployed"


def _record_balance_snapshot(strat_name: str, tc) -> portfolio.Snapshot | None:
    """Confirm this bot's own open trades, then append to its local equity curve.

    Read-only against the broker and never raises: a bookkeeping failure must
    not abort a trading cycle or leave a run marked as errored.
    """
    try:
        trades = db_mod.get_trades_for_ledger()
        open_trades = [t for t in trades if str(t.get("status") or "") == "open"]

        checks = broker_sync.check_open_trades(tc, open_trades)
        for check in checks:
            try:
                db_mod.set_broker_sync(check.trade_id, check.status, check.broker_shares)
            except Exception as exc:
                log.warning("  Broker verdict not stored for trade %s: %s",
                            check.trade_id, exc)

        # Prefer the broker's own price for a position it holds; fall back to
        # the market feed for anything it could not price (e.g. a `missing` row).
        marks = broker_sync.marks_from_checks(checks)
        unmarked = [
            str(t.get("ticker") or "") for t in open_trades
            if str(t.get("ticker") or "") not in marks
        ]
        if unmarked:
            marks.update(broker_sync.fetch_marks(unmarked))

        snap = portfolio.build_snapshot(
            trades, marks,
            strategy=strat_name,
            broker_status=broker_sync.status_map(checks),
            starting_capital=_ledger_capital_base(),
        )
        db_mod.save_balance_snapshot(
            snap.as_dict(), source="bot_run", broker_equity=_broker_equity(tc)
        )

        log.info(
            "Bot ledger: equity $%.2f | realized $%+.2f | unrealized $%+.2f | "
            "total $%+.2f (%+.2f%% on $%.2f base) | %d open, %d closed",
            snap.equity, snap.realized_pnl, snap.unrealized_pnl, snap.total_pnl,
            snap.total_return_pct * 100, snap.starting_capital,
            snap.open_count, snap.closed_count,
        )
        if not snap.marks_complete:
            log.warning("  Some positions had no price mark — equity is a floor")
        if snap.broker_mismatched:
            log.warning("  %d open trade(s) did not match the broker — see "
                        "broker_status on the trades table", snap.broker_mismatched)
        return snap
    except Exception as exc:
        log.warning("Balance snapshot not recorded (%s)", exc)
        return None


def build_pnl_report(strategy: str | None = None) -> str:
    """Human-readable lifetime P&L, computed entirely from the local database.

    Marks for open positions are fetched live when reachable; without them the
    unrealized leg reads as a floor rather than a guess.
    """
    trades = db_mod.get_trades_for_ledger()
    open_trades = [t for t in trades if str(t.get("status") or "") == "open"]

    checks: list = []
    marks: dict[str, float] = {}
    try:
        checks = broker_sync.check_open_trades(_get_trading(), open_trades)
        marks = broker_sync.marks_from_checks(checks)
    except Exception as exc:
        log.warning("Broker unreachable for the report (%s)", exc)
    unmarked = [
        str(t.get("ticker") or "") for t in open_trades
        if str(t.get("ticker") or "") not in marks
    ]
    if unmarked:
        marks.update(broker_sync.fetch_marks(unmarked))

    snap = portfolio.build_snapshot(
        trades, marks,
        strategy=strategy,
        broker_status=broker_sync.status_map(checks) if checks else None,
        starting_capital=_ledger_capital_base(),
    )

    # ASCII only: this prints to the Windows console, which is cp1252 and
    # raises UnicodeEncodeError on box-drawing characters and em dashes.
    w = 62
    lines = [
        "=" * w,
        "  BOT V2 - LIFETIME P&L (local ledger, this bot's trades only)",
        "=" * w,
        f"  Capital base ({_ledger_capital_label()})  ${snap.starting_capital:>14,.2f}",
        f"  Realized P&L                  ${snap.realized_pnl:>+14,.2f}",
        f"  Unrealized P&L                ${snap.unrealized_pnl:>+14,.2f}",
        "  " + "-" * (w - 4),
        f"  TOTAL P&L                     ${snap.total_pnl:>+14,.2f}",
        f"  TOTAL RETURN                   {snap.total_return_pct * 100:>+14.2f}%",
        f"  Bot equity                    ${snap.equity:>14,.2f}",
        "",
        f"  Closed trades  {snap.closed_count:<6}  wins {snap.wins}  losses {snap.losses}"
        f"  win rate {snap.win_rate * 100:.1f}%",
        f"  Profit factor  {snap.profit_factor:<6.2f}  avg/trade {snap.avg_pnl_pct * 100:+.2f}%",
        f"  Best trade     ${snap.best_trade:+,.2f}     Worst ${snap.worst_trade:+,.2f}",
        f"  Deployed total ${snap.total_deployed:,.2f}  "
        f"(return on deployed {snap.return_on_deployed * 100:+.2f}%)",
        f"  Closed by other bots: {snap.interference_count} "
        f"(${snap.interference_pnl:+,.2f}, in P&L, excluded from stats above)",
        f"  First trade    {snap.first_trade_at or 'n/a'}",
    ]

    if snap.positions:
        lines += ["", f"  OPEN POSITIONS ({snap.open_count})", "  " + "-" * (w - 4)]
        for p in snap.positions:
            if p.unrealized_pnl is None:
                pnl_txt = "        (no mark)"
            else:
                pnl_txt = f"${p.unrealized_pnl:>+10,.2f} {p.unrealized_pct * 100:>+6.2f}%"
            lines.append(
                f"  {p.ticker:<6} {p.shares:>6.0f} @ ${p.entry_price:>8,.2f}  "
                f"{pnl_txt}  [{p.broker_status}]"
            )

    if not snap.marks_complete:
        lines += ["", "  NOTE: a position had no price mark — unrealized P&L is a floor."]
    if snap.broker_mismatched:
        lines += [f"  NOTE: {snap.broker_mismatched} open trade(s) unconfirmed by Alpaca."]
    lines.append("=" * w)
    return "\n".join(lines)


def _daily_closes_for(tickers: list[str], start: date, end: date) -> dict[str, dict[str, float]]:
    """Daily closes per ticker, keyed YYYY-MM-DD. Missing data yields {}."""
    out: dict[str, dict[str, float]] = {}
    for ticker in sorted(set(tickers)):
        try:
            df = data_feed.fetch_bars(
                ticker, start - timedelta(days=5), end, timeframe="1d"
            )
        except Exception as exc:
            log.warning("  %s: daily bars unavailable (%s)", ticker, exc)
            continue
        if df.empty or "close" not in df:
            continue
        series = {}
        for ts, close in df["close"].items():
            try:
                series[pd.Timestamp(ts).strftime("%Y-%m-%d")] = float(close)
            except (TypeError, ValueError):
                continue
        out[ticker] = series
    return out


def rebuild_balance_history(strategy: str | None = None) -> int:
    """Backfill the daily equity curve from trade history and daily closes.

    Marks open positions at each day's close rather than only stepping on exits,
    so the curve shows the drawdowns a realized-only curve hides. Rebuilt points
    carry source='rebuilt' and are the only rows a later rebuild may replace.
    """
    trades = db_mod.get_trades_for_ledger()
    real = [t for t in trades if portfolio.is_real_trade(t)]
    if not real:
        return 0

    entries = [portfolio._parse_ts(t.get("entry_date")) for t in real]
    entries = [e for e in entries if e is not None]
    if not entries:
        return 0
    start = min(entries).date()
    tickers = [str(t.get("ticker") or "") for t in real]

    closes = _daily_closes_for(tickers, start, date.today())
    points = portfolio.daily_equity_curve(trades, closes)
    if not points:
        log.warning("No daily closes available — falling back to a realized-only curve")
        points = portfolio.realized_equity_curve(trades)
    return db_mod.replace_rebuilt_balance_history(points, strategy)


def _in_trading_hours() -> bool:
    """True only while the market is actually open (Alpaca clock).

    DAY market orders submitted outside regular hours queue for the next open
    and fill far from the signal price, so the loop trades strictly inside the
    session. The clock also handles holidays and early closes. If the clock is
    unavailable, fall back to a conservative weekday 09:30-16:00 ET window.
    """
    try:
        return bool(_get_trading().get_clock().is_open)
    except Exception as e:
        log.warning("Market clock unavailable (%s) — using ET fallback window", e)
    now_et = datetime.now(_ET)
    if now_et.weekday() >= 5:
        return False
    return _MARKET_OPEN <= now_et.time() < _MARKET_CLOSE


def run_loop(strategy: StrategyType, interval_minutes: int = 30):
    """Run bot in continuous loop while the market is open.

    Passes run every ``interval_minutes`` and additionally just after each
    modelled signal fill time (see ``_seconds_until_next_pass``).
    """
    global _loop_interval
    log.info("Starting Bot V2 loop — %s every %d min", strategy.value, interval_minutes)
    # A signal stays actionable for one pass after its modelled fill time.
    _loop_interval = timedelta(minutes=interval_minutes)
    timeframe = REGISTRY[strategy.value].timeframe
    while True:
        runtime.heartbeat(SERVICE)
        if _in_trading_hours():
            run_once(strategy)
            runtime.heartbeat(SERVICE)
        else:
            now_et = datetime.now(_ET)
            log.info("Outside trading hours (%s ET) — skipping run", now_et.strftime("%H:%M"))
        seconds = _seconds_until_next_pass(
            datetime.now(timezone.utc), _loop_interval, timeframe
        )
        log.info("Sleeping %.1f minutes...", seconds / 60)
        time.sleep(seconds)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Alpaca Swing Bot V2")
    parser.add_argument("--strategy", type=str, default=None,
                        choices=[s.value for s in StrategyType],
                        help="Trading strategy (required)")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=int, default=30, help="Loop interval (min)")
    parser.add_argument("--pnl", action="store_true",
                        help="Print lifetime P&L from the local ledger and exit")
    parser.add_argument("--rebuild-balance-history", action="store_true",
                        help="Backfill the daily equity curve from closed trades and exit")
    args = parser.parse_args()

    # Read-only reporting: no strategy, no PID registration, no trading.
    if args.pnl:
        print(build_pnl_report(args.strategy))
        return
    if args.rebuild_balance_history:
        n = rebuild_balance_history(args.strategy)
        print(f"Rebuilt {n} daily balance point(s) from closed-trade history.")
        return

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
        "market_data": data_feed.market_data_policy(),
        "cmd": "python " + " ".join(sys.argv),
    })

    if args.loop:
        run_loop(strategy, args.interval)
    else:
        sys.exit(run_once(strategy))


if __name__ == "__main__":
    main()
