# Claude Instructions — Alpaca Swing Bot V2

## Starting the dashboard

```powershell
cd C:\Data\ai_projects\alpaca_swing_bot_v2_multi_strategy
.venv\Scripts\activate
python -m uvicorn dashboard.server:app --host 0.0.0.0 --port 8004
```

**Dashboard runs on port 8004.**
- Local: http://localhost:8004
- LAN:   http://192.168.0.191:8004

Routes:
- `/`               — Home tab (KPIs, open positions, recent trades, backtest results)
- `/`               — Strategies tab (all 6 strategy cards with entry rules and 3-year P&L)
- `/backtest-2024`  — Full Plotly report for 2024
- `/backtest-2025`  — Full Plotly report for 2025
- `/backtest-2026`  — Full Plotly report for 2026

Note: run backtests before opening the dashboard or the DB will be empty.

## Starting the bot

```powershell
cd C:\Data\ai_projects\alpaca_swing_bot_v2_multi_strategy
.venv\Scripts\activate

python bot.py                                   # trend_pullback (default)
python bot.py --strategy ensemble               # recommended — best combined P&L
python bot.py --strategy regime
python bot.py --strategy breakout
python bot.py --strategy momentum_macd
python bot.py --strategy mean_reversion
python bot.py --strategy ensemble --loop        # continuous loop every 30 min
python bot.py --strategy ensemble --loop --interval 60
```

One strategy per process. Paper trading only (`paper=True` is hardcoded in `bot.py`).

## RULE: restart after changes

**Whenever you make changes to any dashboard file (`dashboard/server.py`, `dashboard/index.html`, `dashboard/db.py`, `dashboard/bot_hooks.py`) or to `bot.py`, you MUST:**

1. Kill the running process (Ctrl+C equivalent) and restart it.
2. For the **dashboard**: restart with `python -m uvicorn dashboard.server:app --host 0.0.0.0 --port 8004` and post the link `http://localhost:8004` in your response once it is running.
3. For the **bot**: restart with the appropriate `python bot.py --strategy <strategy>` command.
4. Confirm in your response that the restart completed and include the relevant URL.

Do not leave the user without a live server after a dashboard change.
