# Alpaca Swing Bot V2

**Autoresearch-inspired swing trader** — 6 strategies, autonomous experimentation, real market data backtesting.

Built on the foundation of V1 with **3 new V2 strategies** (MACD Momentum, Ensemble, Regime Adaptive) plus a full research loop.

## System

- **Hardware**: ALIENWARE 16 (RTX 5050 4GB, Intel i9)
- **Broker**: Alpaca Paper Trading (paper mode by default)
- **Universe**: NVDA · AMZN · META · AMD · ARM
- **Dashboard**: [http://localhost:8004](http://localhost:8004) · LAN: [http://192.168.0.191:8004](http://192.168.0.191:8004)

## Strategies

One strategy runs at a time — selected via `--strategy` (default: `trend_pullback`). The `ensemble` strategy internally polls all 5 base strategies but still runs as a single bot process.

| Strategy | Type | 2025 P&L | 2026 P&L | Combined |
|----------|------|----------|----------|----------|
| **Trend Pullback** | V1 — dip buying + earnings filter | +$71.37 | +$142.02 | **+$213.39** |
| Breakout | V1 — resistance break | -$26.51 | +$117.66 | +$91.15 |
| Mean Reversion | V1 — oversold bounce | -$26.31 | +$26.68 | +$0.37 |
| MACD Momentum | V2 — MACD cross | +$13.31 | +$35.99 | +$49.30 |
| **Ensemble** | V2 — weighted vote of all 5 | +$199.92 | +$331.78 | **+$531.70** |
| **Regime Adaptive** | V2 — market-regime-aware | +$85.69 | +$239.86 | **+$325.55** |

*Baselines as of 2026-06-02. See `program.md` for full experiment log.*

## Setup

```powershell
# Windows (PowerShell)
cd C:\Data\ai_projects\alpaca_swing_bot_v2_multi_strategy
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Usage

```powershell
# Backtest — all 6 strategies, writes results to swing_bot_v2.db
python backtest_2025.py
python backtest_2026.py

# Dashboard — run backtests first or DB will be empty
python -m uvicorn dashboard.server:app --host 0.0.0.0 --port 8004
# Then open: http://localhost:8004
#   Home tab      — live KPIs, open positions, recent trades, all backtest results
#   Strategies tab — cards for all 6 strategies with entry rules and 3-year P&L
#   2024/2025/2026 Report links — full Plotly HTML reports (routes: /backtest-2024, /backtest-2025, /backtest-2026)

# Live trading — one strategy per process, paper trading only
python bot.py                                    # trend_pullback (default)
python bot.py --strategy breakout
python bot.py --strategy momentum_macd
python bot.py --strategy ensemble
python bot.py --strategy regime
python bot.py --strategy mean_reversion
python bot.py --strategy ensemble --loop         # continuous loop every 30 min
python bot.py --strategy ensemble --loop --interval 60

# Parameter optimization
python -c "from research.optimizer import random_search; from config import StrategyType; r = random_search(StrategyType.TREND_PULLBACK, 2026); print(r[:3])"
```

## Research (autoresearch-inspired)

See `program.md` for the full research program definition. The agent can autonomously:
1. Propose strategy modifications
2. Run backtests on both 2025 AND 2026 data
3. Compare results against baselines in the SQLite DB
4. Keep improvements that work across both years
5. Log experiments for audit trail