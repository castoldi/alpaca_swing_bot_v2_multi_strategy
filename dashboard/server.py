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

_HERE = Path(__file__).resolve().parent
_PROJECT = _HERE.parent
if str(_PROJECT) not in sys.path:
    sys.path.insert(0, str(_PROJECT))

from config import ALPACA_KEY, ALPACA_PAPER, ALPACA_SECRET, PARAMS, TICKERS
from dashboard import db as db_mod
from logger_setup import get_logger

log = get_logger(__name__)

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
    stats["is_paper"] = ALPACA_PAPER
    stats["now"] = datetime.now(timezone.utc).isoformat()
    return stats


@app.get("/api/runs")
async def get_runs(limit: int = Query(50, ge=1, le=200)):
    return {"runs": db_mod.get_recent_runs(limit)}


@app.get("/api/signals")
async def get_signals(limit: int = Query(100, ge=1, le=500)):
    return {"signals": db_mod.get_recent_signals(limit)}


@app.get("/api/backtest-results")
async def backtest_results(year: Optional[int] = None):
    return {"results": db_mod.get_backtest_results(year)}


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