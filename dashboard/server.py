"""FastAPI dashboard for Alpaca Swing Bot V2 — port 8004.

Usage:
    python -m uvicorn dashboard.server:app --host 0.0.0.0 --port 8004
    python dashboard/server.py
"""
from __future__ import annotations

import ipaddress
import math
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

_HERE = Path(__file__).resolve().parent
_PROJECT = _HERE.parent
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))

import config
from config import ALPACA_KEY, ALPACA_PAPER, ALPACA_SECRET, PARAMS, TICKERS, ALL_TICKERS, BAR_TIMEFRAME
from dashboard import db as db_mod
from logger_setup import get_logger
import data_feed
import runtime

log = get_logger(__name__)

# Correlation-id prefix the bot stamps on every Alpaca order it places. Mirrors
# bot.CLIENT_ORDER_PREFIX — it is the proof an order is the bot's own.
CLIENT_ORDER_PREFIX = "swingv2"

_trading_client = None


def _get_trading():
    global _trading_client
    if _trading_client is None:
        if not ALPACA_PAPER:
            log.warning("ALPACA_PAPER=false — forcing paper=True safety override")
        from alpaca.trading.client import TradingClient
        # PAPER ONLY — hardcoded safety
        from http_timeouts import apply_default_timeout
        _trading_client = apply_default_timeout(
            TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)
        )
    return _trading_client


db_mod.set_tickers(ALL_TICKERS)

app = FastAPI(title="Alpaca Swing Bot V2 Dashboard")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── LAN access token ──────────────────────────────────────────────────────────
AUTH_COOKIE = "swing_dash_token"
_AUTH_COOKIE_MAX_AGE = 400 * 24 * 3600   # browsers cap cookies at 400 days


def _is_loopback(host: str | None) -> bool:
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return host == "localhost"


def _token_matches(candidate: str | None, token: str) -> bool:
    return bool(candidate) and secrets.compare_digest(candidate.encode(), token.encode())


@app.middleware("http")
async def require_lan_token(request: Request, call_next):
    """Gate every non-local request behind DASHBOARD_TOKEN when it is set.

    Requests from this machine (the watchdog's health probe, manage.ps1, the
    owner at the desk) are always allowed. A phone opens
    ``/?token=<token>`` once; the token is then kept in a cookie and removed
    from the address bar.
    """
    token = config.DASHBOARD_TOKEN
    if not token or _is_loopback(request.client.host if request.client else None):
        return await call_next(request)
    if _token_matches(request.cookies.get(AUTH_COOKIE), token):
        return await call_next(request)
    if _token_matches(request.query_params.get("token"), token):
        response = RedirectResponse(str(request.url.remove_query_params("token")), status_code=303)
        response.set_cookie(AUTH_COOKIE, token, max_age=_AUTH_COOKIE_MAX_AGE,
                            httponly=True, samesite="lax")
        return response
    return JSONResponse({"error": "unauthorized: open the dashboard link that includes ?token="},
                        status_code=401)


if not config.DASHBOARD_TOKEN:
    log.warning("DASHBOARD_TOKEN is not set — the dashboard is open to the whole LAN")


def _configured_timeframes() -> list[str]:
    """Distinct strategy timeframes in display order."""
    from strategies import get_all
    return list(dict.fromkeys(strategy.timeframe for strategy in get_all()))


def _configured_strategy_count() -> int:
    from strategies import get_all
    return len(get_all())


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/positions")
async def get_positions():
    try:
        tc = _get_trading()
        result = db_mod.sync_positions_from_alpaca(tc)
        return result
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/trades")
async def get_trades(limit: int = Query(200, ge=1, le=1000), status: Optional[str] = None):
    if status == "open":
        return {"trades": db_mod.get_open_trades()}
    elif status == "closed":
        return {"trades": db_mod.get_closed_trades(limit)}
    return {"trades": db_mod.get_all_trades(limit)}


@app.get("/api/summary")
async def get_summary():
    stats = db_mod.portfolio_stats()
    try:
        tc = _get_trading()
        pos_data = db_mod.sync_positions_from_alpaca(tc)
        stats["live_positions"] = pos_data["positions"]
        stats["deployed"] = round(pos_data["deployed"], 2)
    except Exception as e:
        stats["live_positions"] = []
        stats["deployed"] = 0
        stats["sync_error"] = str(e)

    stats["position_size_pct"] = PARAMS.position_size_pct
    stats["initial_backtest_equity"] = PARAMS.initial_backtest_equity
    stats["max_concurrent_positions"] = PARAMS.max_concurrent_positions
    stats["tickers"] = ALL_TICKERS
    stats["timeframe"] = BAR_TIMEFRAME
    stats["timeframes"] = _configured_timeframes()
    stats["strategy_count"] = _configured_strategy_count()
    stats["is_paper"] = ALPACA_PAPER
    stats["now"] = datetime.now(timezone.utc).isoformat()
    return stats


# ── Live bot + market state ───────────────────────────────────────────────────

from zoneinfo import ZoneInfo
from datetime import time as _dtime

_ET = ZoneInfo("America/New_York")
_BOT_OPEN = _dtime(8, 30)
_BOT_CLOSE = _dtime(17, 0)


def _bot_window_open() -> bool:
    """Whether the bot's loop will call run_once now (08:30–17:00 ET)."""
    now_et = datetime.now(_ET).time().replace(second=0, microsecond=0)
    return _BOT_OPEN <= now_et < _BOT_CLOSE


@app.get("/api/bot-status")
async def bot_status():
    """Is the bot looping? Strategy, interval, uptime, last loop age, and whether
    it's inside its trading window — everything the status hero needs."""
    st = runtime.read_status("bot")
    st["bot_window_open"] = _bot_window_open()
    st["now_et"] = datetime.now(_ET).isoformat()
    st["now"] = datetime.now(timezone.utc).isoformat()
    if st.get("healthy") and st.get("age_sec") is not None and st.get("interval"):
        # Rough estimate of the next loop pass.
        st["next_run_in_sec"] = max(0, st["interval"] * 60 - st["age_sec"])
    return st


@app.get("/api/account")
async def get_account():
    """Live Alpaca account snapshot: equity, day P&L, buying power, cash."""
    try:
        tc = _get_trading()
        acct = await run_in_threadpool(tc.get_account)
        equity = float(acct.equity)
        last_equity = float(acct.last_equity) if acct.last_equity else equity
        day_pl = equity - last_equity
        return {
            "equity": round(equity, 2),
            "last_equity": round(last_equity, 2),
            "day_pl": round(day_pl, 2),
            "day_pl_pct": round(day_pl / last_equity * 100, 2) if last_equity else 0.0,
            "buying_power": round(float(acct.buying_power), 2),
            "cash": round(float(acct.cash), 2),
            "portfolio_value": round(float(acct.portfolio_value), 2),
            "currency": acct.currency,
            "status": str(acct.status),
            "is_paper": ALPACA_PAPER,
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/market")
async def get_market():
    """Universe snapshots (last price + day change) plus the Alpaca market clock."""
    result: dict = {"tickers": ALL_TICKERS}
    try:
        result["snapshots"] = await run_in_threadpool(data_feed.fetch_snapshots, ALL_TICKERS)
    except Exception as e:
        result["snapshots"] = {}
        result["snapshot_error"] = str(e)

    try:
        tc = _get_trading()
        clock = await run_in_threadpool(tc.get_clock)
        result["clock"] = {
            "is_open": bool(clock.is_open),
            "next_open": clock.next_open.isoformat() if clock.next_open else None,
            "next_close": clock.next_close.isoformat() if clock.next_close else None,
            "timestamp": clock.timestamp.isoformat() if clock.timestamp else None,
        }
    except Exception as e:
        result["clock"] = None
        result["clock_error"] = str(e)

    result["bot_window_open"] = _bot_window_open()
    return result


def _enum_text(value) -> str:
    return str(getattr(value, "value", value) or "").split(".")[-1].lower()


def _order_row(order, kind: str, client_order_id: str) -> dict:
    submitted = getattr(order, "submitted_at", None) or getattr(order, "created_at", None)
    filled_qty = getattr(order, "filled_qty", None)
    return {
        "symbol": getattr(order, "symbol", None),
        "kind": kind,                       # entry / protect / exit / tp / stop
        "side": _enum_text(getattr(order, "side", "")),
        "type": _enum_text(getattr(order, "order_type", None) or getattr(order, "type", "")),
        "qty": float(getattr(order, "qty", 0) or 0),
        "filled_qty": float(filled_qty) if filled_qty else 0.0,
        "filled_avg_price": float(order.filled_avg_price) if getattr(order, "filled_avg_price", None) else None,
        "limit_price": float(order.limit_price) if getattr(order, "limit_price", None) else None,
        "stop_price": float(order.stop_price) if getattr(order, "stop_price", None) else None,
        "status": _enum_text(getattr(order, "status", "")),
        "submitted_at": submitted.isoformat() if hasattr(submitted, "isoformat") else submitted,
        "client_order_id": client_order_id,
    }


def bot_order_rows(raw_orders) -> list[dict]:
    """Flatten the bot's own parent orders and their bracket/OCO child legs.

    Child legs carry broker-generated client ids, so a prefix filter alone drops
    every stop and take-profit fill. They are ours because their parent is;
    each leg is listed under its parent's correlation id.
    """
    rows: list[dict] = []
    for order in raw_orders or []:
        coid = str(getattr(order, "client_order_id", "") or "")
        if not coid.startswith(CLIENT_ORDER_PREFIX):
            continue
        # coid shape: swingv2-<kind>-<strategy>-<ticker>-<hex>
        parts = coid.split("-")
        rows.append(_order_row(order, parts[1] if len(parts) > 1 else "?", coid))
        for leg in getattr(order, "legs", None) or []:
            leg_type = _enum_text(getattr(leg, "order_type", None) or getattr(leg, "type", ""))
            rows.append(_order_row(leg, "stop" if "stop" in leg_type else "tp", coid))
    rows.sort(key=lambda row: str(row["submitted_at"] or ""), reverse=True)
    return rows


@app.get("/api/bot-orders")
async def bot_orders(limit: int = Query(50, ge=1, le=200)):
    """Recent Alpaca orders THIS bot placed, including bracket exit legs.

    The key is shared, so the query is narrowed to the bot's own symbols
    before Alpaca's 500-order page limit applies; ownership is still proved
    by the swingv2 correlation-id prefix on each parent.
    """
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        def _fetch():
            tc = _get_trading()
            req = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500, nested=True,
                                   symbols=list(ALL_TICKERS))
            return tc.get_orders(filter=req) or []

        raw = await run_in_threadpool(_fetch)
        return {"orders": bot_order_rows(raw)[:limit], "prefix": CLIENT_ORDER_PREFIX}
    except Exception as e:
        return JSONResponse({"error": str(e), "orders": []}, status_code=500)


def _utc(value) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def execution_quality(trades: list[dict]) -> dict:
    """Live fills against the backtest's execution model, plus medians.

    delay: broker fill time minus the modelled fill minute. Slippage vs model:
    fill price against that minute's open (what the backtest paid). Slippage
    vs signal: fill price against the signal bar close the SL/TP came from.
    """
    import statistics

    rows = []
    for t in trades:
        modelled, filled = _utc(t.get("modelled_fill_at")), _utc(t.get("entry_filled_at"))
        price, model, signal = (t.get("entry_filled_price"), t.get("model_fill_price"),
                                t.get("entry_price"))
        rows.append({
            "id": t.get("id"), "ticker": t.get("ticker"), "strategy": t.get("strategy"),
            "modelled_fill_at": modelled.isoformat() if modelled else None,
            "entry_filled_at": filled.isoformat() if filled else None,
            "delay_sec": round((filled - modelled).total_seconds(), 1) if modelled and filled else None,
            "fill_price": price, "model_fill_price": model, "signal_close": signal,
            "slip_vs_model_pct": round((price / model - 1) * 100, 3) if price and model else None,
            "slip_vs_signal_pct": round((price / signal - 1) * 100, 3) if price and signal else None,
        })

    def median(key):
        values = [r[key] for r in rows if r[key] is not None]
        return round(statistics.median(values), 3) if values else None

    return {"trades": rows, "median_delay_sec": median("delay_sec"),
            "median_slip_vs_model_pct": median("slip_vs_model_pct"),
            "median_slip_vs_signal_pct": median("slip_vs_signal_pct")}


@app.get("/api/execution-quality")
async def get_execution_quality(limit: int = Query(30, ge=1, le=500)):
    """How far live entries land from the backtest's modelled fill."""
    return execution_quality(db_mod.get_execution_quality(limit))


@app.get("/api/pnl")
async def get_pnl():
    """This bot's own P&L, computed from the local ledger.

    Distinct from /api/account, which reports the whole Alpaca account — that key
    is shared with other projects, so its equity is not this bot's performance.
    Open positions are marked with live prices when reachable; when a mark is
    missing, `marks_complete` is false and unrealized P&L reads as a floor.
    """
    import portfolio
    import broker_sync

    trades = db_mod.get_trades_for_ledger()
    open_trades = [t for t in trades if str(t.get("status") or "") == "open"]

    checks: list = []
    marks: dict = {}
    try:
        tc = _get_trading()
        checks = await run_in_threadpool(broker_sync.check_open_trades, tc, open_trades)
        marks = broker_sync.marks_from_checks(checks)
    except Exception as e:
        marks = {}
        checks = []
        sync_error = str(e)
    else:
        sync_error = None

    unmarked = [
        str(t.get("ticker") or "") for t in open_trades
        if str(t.get("ticker") or "") not in marks
    ]
    if unmarked:
        marks.update(await run_in_threadpool(broker_sync.fetch_marks, unmarked))

    allocation = PARAMS.bot_capital_allocation
    use_allocation = math.isfinite(allocation) and allocation > 0
    snap = portfolio.build_snapshot(
        trades, marks,
        broker_status=broker_sync.status_map(checks) if checks else None,
        starting_capital=allocation if use_allocation else None,
    )
    payload = snap.as_dict()
    payload["capital_base_method"] = "allocation" if use_allocation else "peak_deployed"
    if sync_error:
        payload["sync_error"] = sync_error
    return payload


@app.get("/api/balance-history")
async def balance_history(
    limit: int = Query(500, ge=1, le=5000),
    daily: bool = True,
):
    """The bot's own equity curve. `daily=true` returns one point per day."""
    rows = (
        db_mod.get_daily_balance_history(limit) if daily
        else db_mod.get_balance_history(limit)
    )
    latest = db_mod.get_latest_balance()
    base = float(latest["starting_capital"]) if latest and latest["starting_capital"] else 0.0
    # Percentages are derived here, not stored: the capital base is a running
    # maximum, so a stored percentage would go stale the moment it grows.
    for r in rows:
        pnl = float(r.get("realized_pnl") or 0) + float(r.get("unrealized_pnl") or 0)
        r["total_pnl"] = round(pnl, 2)
        r["return_pct"] = round(pnl / base * 100, 4) if base else 0.0
    return {"history": rows, "starting_capital": base, "daily": daily}


@app.get("/api/equity-curves")
async def equity_curves(from_year: Optional[int] = Query(None, alias="from")):
    """Growth of $1 per strategy, chained across the annual backtest resets.

    `from` rebases the race to that year, so each strategy restarts at 1.0 and
    the chart answers "what if I had started here". Built by
    `scripts/build_equity_curves.py`; empty until that has run.
    """
    curves = db_mod.get_equity_curves(from_year)
    years = db_mod.get_equity_curve_years()
    series = []
    for name, points in sorted(curves.items()):
        if not points:
            continue
        series.append({
            "strategy": name,
            "final": points[-1]["growth"],
            "points": points,
        })
    # Best final multiple first — the chart labels lines in this order.
    series.sort(key=lambda s: s["final"], reverse=True)
    return {
        "series": series,
        "years": years,
        "from_year": from_year or (years[0] if years else None),
    }


@app.get("/api/runs")
async def get_runs(limit: int = Query(50, ge=1, le=200)):
    return {"runs": db_mod.get_recent_runs(limit)}


@app.get("/api/signals")
async def get_signals(limit: int = Query(100, ge=1, le=500)):
    return {"signals": db_mod.get_recent_signals(limit)}


@app.get("/api/backtest-results")
async def backtest_results(year: Optional[int] = None):
    return {
        "results": db_mod.get_backtest_results(year),
        "timeframe": BAR_TIMEFRAME,
        "timeframes": _configured_timeframes(),
    }


@app.get("/api/backtest-history")
async def backtest_history(limit: int = Query(200, ge=1, le=1000), year: Optional[int] = None):
    """Full historical log of every backtest run (all timeframes, timestamped)."""
    return {"history": db_mod.get_backtest_history(limit, year)}


@app.get("/api/strategy-examples")
async def strategy_examples(refresh: bool = False):
    """Real recent entry examples per strategy (candles + entry/SL/TP/exit).

    Heavy (yfinance fetch + scan) but cached, so it runs in a threadpool to keep
    the event loop responsive.
    """
    from dashboard import strategy_examples as se
    try:
        return await run_in_threadpool(se.get_examples, refresh)
    except Exception as e:
        return JSONResponse({"error": str(e), "examples": {}}, status_code=500)


@app.get("/api/strategies")
async def get_strategies():
    """All registered strategies with metadata and latest backtest P&L per year."""
    from strategies import get_all
    bt_results = db_mod.get_backtest_results()
    pnl: dict[str, dict[int, float]] = {}
    for r in bt_results:
        pnl.setdefault(r["strategy"], {})[r["year"]] = r["total_pnl"]

    out = []
    for strat in get_all():
        m = strat.meta()
        m["pnl"] = pnl.get(strat.name, {})
        out.append(m)
    return {"strategies": out}


@app.get("/api/experiments")
async def experiments(limit: int = Query(50, ge=1, le=200)):
    return {"experiments": db_mod.get_experiments(limit)}


@app.get("/api/tax")
async def tax_summary(year: int | None = None):
    """Realized tax picture, recomputed from the full trade history."""
    import tax as tax_mod
    from config import PARAMS

    try:
        db_mod.rebuild_tax_records()
    except Exception:
        pass  # stale records are better than a broken page

    trades = db_mod.get_all_trades(limit=5000)
    records = tax_mod.compute_tax_records(
        trades,
        mtm_475f=PARAMS.tax_mtm_475f,
        identical_groups=PARAMS.tax_identical_groups,
        crypto_symbols=PARAMS.tax_crypto_symbols,
    )
    lots, disposals = tax_mod.build_lot_ledger(trades, PARAMS.tax_lot_method)
    tax_mod.apply_wash_basis_adjustments(lots, records)
    now = datetime.now(timezone.utc)
    target = year or now.year

    years = sorted(
        {
            d.year
            for d in (tax_mod.parse_dt(r.sale_date) for r in records)
            if d is not None
        },
        reverse=True,
    )
    summary = tax_mod.summarize_year(
        records, target, PARAMS.tax_short_term_rate, PARAMS.tax_long_term_rate,
        use_brackets=PARAMS.tax_use_brackets,
        filing_status=PARAMS.tax_filing_status,
        other_income=PARAMS.tax_other_income,
        apply_niit=PARAMS.tax_niit,
        estimated_payments=PARAMS.tax_estimated_payments,
        mtm_475f=PARAMS.tax_mtm_475f,
    )

    by_ticker: dict[str, dict] = {}
    for r in records:
        sold = tax_mod.parse_dt(r.sale_date)
        if sold is None or sold.year != target:
            continue
        slot = by_ticker.setdefault(
            r.ticker,
            {"ticker": r.ticker, "trades": 0, "realized": 0.0,
             "wash_sales": 0, "disallowed": 0.0},
        )
        slot["trades"] += 1
        slot["realized"] += r.realized_pnl
        slot["wash_sales"] += int(r.is_wash_sale)
        slot["disallowed"] += r.disallowed_loss
    for slot in by_ticker.values():
        slot["realized"] = round(slot["realized"], 2)
        slot["disallowed"] = round(slot["disallowed"], 2)

    guard_active = (now.month, now.day) >= (
        PARAMS.tax_guard_start_month, PARAMS.tax_guard_start_day
    )

    return {
        "summary": summary,
        "years": years or [target],
        "by_ticker": sorted(by_ticker.values(), key=lambda s: s["realized"]),
        "records": [r.as_dict() for r in records if
                    (lambda d: d is not None and d.year == target)(
                        tax_mod.parse_dt(r.sale_date))][:300],
        "guard": {
            "enabled": PARAMS.tax_year_end_guard,
            "active_now": bool(PARAMS.tax_year_end_guard and guard_active),
            "starts": f"{PARAMS.tax_guard_start_month:02d}-{PARAMS.tax_guard_start_day:02d}",
            "hard_block": PARAMS.tax_hard_block,
            "hard_block_days": PARAMS.tax_hard_block_days,
        },
        "election": {
            "mtm_475f": PARAMS.tax_mtm_475f,
            "identical_groups": [list(g) for g in PARAMS.tax_identical_groups],
            "crypto_symbols": list(PARAMS.tax_crypto_symbols),
            "lot_method": PARAMS.tax_lot_method,
            "uses_brackets": PARAMS.tax_use_brackets,
            "filing_status": PARAMS.tax_filing_status,
            "niit": PARAMS.tax_niit,
        },
        "lots": [
            l.as_dict() for l in lots
            if not l.closed or l.basis_adjustment
        ][:200],
        "open_lot_count": sum(1 for l in lots if not l.closed),
        "paper_account": True,
        "generated_at": now.isoformat(),
    }


@app.get("/api/research/program")
async def research_program():
    program_path = _PROJECT / "program.md"
    if program_path.exists():
        return {"program": program_path.read_text(encoding="utf-8")}
    return {"program": "No program.md found — create one in the project root."}


# ── Serve dashboard HTML ──────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return (_HERE / "index.html").read_text(encoding="utf-8")


@app.get("/tax", response_class=HTMLResponse)
async def tax_page():
    return (_HERE / "tax.html").read_text(encoding="utf-8")


_NAV_BAR = """<div style="position:sticky;top:0;z-index:999;background:#0f1117;border-bottom:1px solid #2a2d35;padding:10px 24px;display:flex;align-items:center;gap:16px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
  <a href="/" style="color:#60a5fa;font-size:13px;font-weight:600;text-decoration:none">← Dashboard</a>
  <span style="color:#2a2d35">|</span>
  <a href="/backtest-2024" style="color:#8892a4;font-size:13px;font-weight:600;text-decoration:none">2024</a>
  <a href="/backtest-2025" style="color:#8892a4;font-size:13px;font-weight:600;text-decoration:none">2025</a>
  <a href="/backtest-2026" style="color:#8892a4;font-size:13px;font-weight:600;text-decoration:none">2026</a>
  <a href="/tax" style="color:#8892a4;font-size:13px;font-weight:600;text-decoration:none">Tax</a>
</div>"""


def _serve_report(path: Path, year: int) -> HTMLResponse:
    if not path.exists():
        return HTMLResponse(
            f"<html><body style='background:#0f1117;color:#e1e7ef;font-family:sans-serif;padding:40px;text-align:center'>"
            f"<h1>No {year} backtest data</h1><p>Run <code>python backtest_{year}.py</code> first.</p>"
            f"<a href='/' style='color:#60a5fa'>← Dashboard</a></body></html>",
            status_code=404,
        )
    html = path.read_text(encoding="utf-8")
    html = html.replace("<body>", f"<body>{_NAV_BAR}", 1)
    return HTMLResponse(html)


@app.get("/backtest-2024", response_class=HTMLResponse)
async def backtest_2024():
    return _serve_report(_PROJECT / "reports" / "backtest_2024.html", 2024)


@app.get("/backtest-2025", response_class=HTMLResponse)
async def backtest_2025():
    return _serve_report(_PROJECT / "reports" / "backtest_2025.html", 2025)


@app.get("/backtest-2026", response_class=HTMLResponse)
async def backtest_2026():
    return _serve_report(_PROJECT / "reports" / "backtest_2026.html", 2026)


# ── Status endpoint ────────────────────────────────────────────────────────

from datetime import datetime, timezone

@app.get("/status")
async def status():
    port = int(os.getenv("DASHBOARD_PORT", "8004"))
    return {
        "agent": "Alpaca Swing Bot V2",
        "status": "online",
        "port": port,
        "version": "2.0.0",
        "endpoints": {
            "dashboard": f"http://192.168.0.191:{port}",
            "status": f"http://192.168.0.191:{port}/status",
        },
        "check_time": datetime.now(timezone.utc).isoformat(),
        "data_last_updated": db_mod.get_last_bot_run_at(),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import uvicorn
    port = int(os.getenv("DASHBOARD_PORT", "8004"))
    print(f"Alpaca Swing Bot V2 Dashboard: http://0.0.0.0:{port}   (paper={ALPACA_PAPER})")
    uvicorn.run("dashboard.server:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    main()
