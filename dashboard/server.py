"""FastAPI dashboard for Alpaca Swing Bot V2 — port 8004.

Usage:
    python -m uvicorn dashboard.server:app --host 0.0.0.0 --port 8004
    python dashboard/server.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

_HERE = Path(__file__).resolve().parent
_PROJECT = _HERE.parent
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))

from config import ALPACA_KEY, ALPACA_PAPER, ALPACA_SECRET, PARAMS, TICKERS, BAR_TIMEFRAME
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
        _trading_client = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)
    return _trading_client


db_mod.set_tickers(TICKERS)

app = FastAPI(title="Alpaca Swing Bot V2 Dashboard")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


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

    stats["max_capital"] = PARAMS.max_concurrent_capital
    stats["dollars_per_trade"] = PARAMS.dollars_per_trade
    stats["tickers"] = TICKERS
    stats["timeframe"] = BAR_TIMEFRAME
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
    result: dict = {"tickers": TICKERS}
    try:
        result["snapshots"] = await run_in_threadpool(data_feed.fetch_snapshots, TICKERS)
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


@app.get("/api/bot-orders")
async def bot_orders(limit: int = Query(50, ge=1, le=200)):
    """Recent Alpaca orders THIS bot placed — filtered by the swingv2 correlation
    id prefix. Proves opens/closes are the bot's own, straight from the broker."""
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        def _fetch():
            tc = _get_trading()
            req = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=300, nested=False)
            return tc.get_orders(filter=req) or []

        raw = await run_in_threadpool(_fetch)
        orders = []
        for o in raw:
            coid = str(getattr(o, "client_order_id", "") or "")
            if not coid.startswith(CLIENT_ORDER_PREFIX):
                continue
            # coid shape: swingv2-<kind>-<strategy>-<ticker>-<hex>
            parts = coid.split("-")
            kind = parts[1] if len(parts) > 1 else "?"
            submitted = getattr(o, "submitted_at", None) or getattr(o, "created_at", None)
            filled_qty = getattr(o, "filled_qty", None)
            orders.append({
                "symbol": getattr(o, "symbol", None),
                "kind": kind,                       # entry / tp1 / tp2 / tp3 / stop / exit
                "side": str(getattr(o, "side", "")).split(".")[-1].lower(),
                "type": str(getattr(o, "order_type", getattr(o, "type", ""))).split(".")[-1].lower(),
                "qty": float(getattr(o, "qty", 0) or 0),
                "filled_qty": float(filled_qty) if filled_qty else 0.0,
                "filled_avg_price": float(o.filled_avg_price) if getattr(o, "filled_avg_price", None) else None,
                "limit_price": float(o.limit_price) if getattr(o, "limit_price", None) else None,
                "stop_price": float(o.stop_price) if getattr(o, "stop_price", None) else None,
                "status": str(getattr(o, "status", "")).split(".")[-1].lower(),
                "submitted_at": submitted.isoformat() if submitted else None,
                "client_order_id": coid,
            })
        orders.sort(key=lambda x: x["submitted_at"] or "", reverse=True)
        return {"orders": orders[:limit], "prefix": CLIENT_ORDER_PREFIX}
    except Exception as e:
        return JSONResponse({"error": str(e), "orders": []}, status_code=500)


@app.get("/api/runs")
async def get_runs(limit: int = Query(50, ge=1, le=200)):
    return {"runs": db_mod.get_recent_runs(limit)}


@app.get("/api/signals")
async def get_signals(limit: int = Query(100, ge=1, le=500)):
    return {"signals": db_mod.get_recent_signals(limit)}


@app.get("/api/backtest-results")
async def backtest_results(year: Optional[int] = None):
    return {"results": db_mod.get_backtest_results(year), "timeframe": BAR_TIMEFRAME}


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


_NAV_BAR = """<div style="position:sticky;top:0;z-index:999;background:#0f1117;border-bottom:1px solid #2a2d35;padding:10px 24px;display:flex;align-items:center;gap:16px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
  <a href="/" style="color:#60a5fa;font-size:13px;font-weight:600;text-decoration:none">← Dashboard</a>
  <span style="color:#2a2d35">|</span>
  <a href="/backtest-2024" style="color:#8892a4;font-size:13px;font-weight:600;text-decoration:none">2024</a>
  <a href="/backtest-2025" style="color:#8892a4;font-size:13px;font-weight:600;text-decoration:none">2025</a>
  <a href="/backtest-2026" style="color:#8892a4;font-size:13px;font-weight:600;text-decoration:none">2026</a>
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