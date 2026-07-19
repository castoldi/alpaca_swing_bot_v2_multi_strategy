# Alpaca Swing Bot V2 — Multi-Strategy

> [!WARNING]
> **DISCLAIMER — PLEASE READ BEFORE USE**
>
> This project is a **Beta proof-of-concept** built for learning and research purposes only. It is **not ready for real live trading** and should not be used with real money.
>
> - This software was **not built by a financial professional, trading expert, or licensed investment advisor**.
> - Nothing in this repository constitutes financial advice, investment advice, or a recommendation to buy or sell any security.
> - Past backtest performance does not guarantee future results. Backtests are simulated and do not account for slippage, commissions, liquidity constraints, or real-world execution delays.
> - Use this software entirely at your own risk. The authors accept no responsibility for any financial losses incurred.
>
> **Always consult a qualified financial professional before making any investment decisions.**

---

An automated swing trading bot that trades a focused universe of high-momentum US equities using seven strategies. Six strategies use 4-hour candles; SMA 50 Cross uses completed daily candles. All execution is through the Alpaca brokerage API (paper trading by default) with a live FastAPI dashboard.

---

## What Is Swing Trading?

Swing trading sits between day trading and long-term investing. Most positions are held for **2–7 trading days**; SMA 50 Cross holds until the daily trend crosses back below its average or its emergency stop fills.

Each trade has three pre-defined levels set at entry:

| Level | Purpose |
|-------|---------|
| **Entry price** | The price at which the position opens |
| **Stop loss** | The maximum loss accepted — trade exits automatically if price falls here |
| **Take profit** | An optional target used by the six bracket-exit strategies |

Every trade has a broker-held stop. SMA 50 Cross deliberately has no fixed profit target because its exit is the opposite moving-average cross.

---

## Trading Universe

Five large-cap, high-liquidity technology stocks:

| Ticker | Company | Why |
|--------|---------|-----|
| **NVDA** | NVIDIA | High daily range, strong trend behaviour |
| **AMZN** | Amazon | Consistent swing structure, deep liquidity |
| **META** | Meta Platforms | Clear trend + reversion patterns |
| **AMD** | Advanced Micro Devices | High beta, strong momentum moves |
| **ARM** | Arm Holdings | Volatile, momentum-driven, swing-friendly |

Each stock is traded independently. The bot holds at most one position per ticker at a time.

---

## Position Sizing & Risk Limits

| Parameter | Value |
|-----------|-------|
| Capital per trade | 20% of current account equity, capped by available cash |
| Max concurrent positions | 5 (no margin) |
| Annual backtest starting equity | $1,000, reset for every calendar year |
| Default stop loss | 7–10% below entry (strategy-dependent) |
| Default take profit | 3–12% above entry for bracket strategies; none for SMA 50 Cross |
| Max holding period | 3–7 days for bracket strategies; cross-driven for SMA 50 Cross |

For the six bracket strategies, take-profit targets are **dynamically sized** using Average True Range (ATR). SMA 50 Cross bypasses that filter because it has no profit target.

---

## Strategies

### 1. Trend Pullback (V1)

**The idea:** Buy a temporary dip in a stock that is already in a confirmed uptrend.

**Business logic:**
- The stock must be trading above its 50-day moving average (uptrend confirmed)
- RSI must have dipped below 55 at some point in the last 10 days (a brief pullback occurred)
- Today's close must be higher than yesterday's close *and* RSI must be rising — indicating the dip is ending and buying pressure is returning
- **Earnings avoidance:** The strategy skips entries within 3 trading days of a known earnings date to avoid gap risk

**In plain terms:** Wait for a strong stock to take a breather, then buy the moment it starts recovering — don't fight the trend, ride it.

**Exit:** 10% stop loss · 3–8% take profit (2× ATR) · 5-day time stop

---

### 2. Breakout (V1)

**The idea:** Buy when a stock breaks to a new high, signalling that buyers have overwhelmed sellers at a prior resistance level.

**Business logic:**
- Price must close above the highest high seen in the last 20 days
- Volume must be at least 1.5× the 20-day average volume (conviction confirmation — big players are involved)
- The daily price range must not be unusually wide (avoids chasing exhaustion moves)
- RSI must be above 50 and rising (momentum is with the breakout)

**In plain terms:** Stocks often consolidate below a price ceiling for weeks, then burst through when demand surges. This strategy catches that burst early with volume as evidence that the move is real.

**Exit:** 8% stop loss · 5–15% take profit (3× ATR) · 7-day time stop

---

### 3. Mean Reversion (V1)

**The idea:** Buy a stock that has sold off sharply but remains in an overall uptrend — betting that the sell-off is temporary.

**Business logic:**
- The stock must still be above its 50-day moving average (long-term uptrend intact)
- RSI must have been below 50 recently (oversold condition)
- Price must be trading below its 20-day moving average by at least a small margin (stretched to the downside)
- Today must show a bounce — today's close above yesterday's close

**In plain terms:** Even strong stocks occasionally get hit by bad news, profit-taking, or market-wide sell-offs. If the underlying trend is intact, these dips often recover quickly. This strategy fades the fear.

**Exit:** 7% stop loss · 1.5–5% take profit (1.5× ATR) · 3-day time stop

---

### 4. MACD Momentum (V2)

**The idea:** Enter when short-term price momentum crosses above long-term momentum — a classic signal that a new upswing is beginning.

**Business logic:**
- MACD histogram turns positive (the 12-day EMA crosses above the 26-day EMA) — the exact crossover bar
- RSI must be above 50 and rising (confirming the momentum shift)
- Price must be above both the 50-day and 20-day moving averages (both trend filters aligned)
- Volume must not be very low (avoids false signals on thin trading days)

**In plain terms:** MACD is a momentum speedometer. When short-term speed exceeds long-term speed for the first time, it often marks the beginning of a sustained move. This strategy catches that ignition point.

**Exit:** 9% stop loss · 4–12% take profit (2.5× ATR) · 6-day time stop

---

### 5. Regime Adaptive (V2)

**The idea:** Adapt entry criteria and risk tolerance based on whether the current market environment is bullish (risk-on), bearish (risk-off), or neutral.

**Regime detection:**
| Regime | Condition | Behaviour |
|--------|-----------|-----------|
| **Risk-On** | Short EMA > Long EMA and price > 50-day SMA | Aggressive: buy dips in strong uptrend, wider stop loss |
| **Risk-Off** | Short EMA < Long EMA and price < 50-day SMA | Defensive: only buy deep oversold bounces (RSI < 40) |
| **Neutral** | Mixed signals | Standard: require close above 50-day SMA with RSI > 40 and green day |

**In plain terms:** Markets go through phases. In a bull phase, you lean in aggressively. In a bear phase, you wait for extreme oversold conditions and use tighter risk. In between, you trade normally. Most strategies ignore this context entirely — this one doesn't.

**Exit:** 10% stop loss (risk-on: 12%, risk-off: 10%) · 3–8% take profit · 5-day time stop

---

### 6. SMA 50 Cross (V2)

**The idea:** Follow the simplest possible daily trend rule: buy a fresh close above the 50-day simple moving average and sell a fresh close below it.

**Business logic:**
- Use completed daily candles only; the current session never generates a signal
- Buy when the previous close was at or below its SMA(50) and the latest close is above its SMA(50)
- Sell when the previous close was at or above its SMA(50) and the latest close is below its SMA(50)
- Attach a broker-held stop 10% below the live entry reference
- Stay long-only; do not open short positions

**In plain terms:** Enter once when the daily trend turns up, stay in while price remains above its 50-day average, and leave when that trend turns down. Exact-cross checks prevent a new order every day price remains above the line.

**Exit:** 10% emergency stop · daily cross below SMA(50) · no take profit · no time stop

---

### 7. Ensemble (V2) — Recommended

**The idea:** Instead of picking one strategy, run all five simultaneously and only trade when multiple strategies agree — reducing false signals and improving confidence.

**How it works:**
Each of the five base strategies casts a vote (signal or no signal) on each bar. Votes are **weighted by historical P&L performance**:

| Strategy | Weight | Rationale |
|----------|--------|-----------|
| Regime Adaptive | 35% | Best overall performer (+$325 combined) |
| MACD Momentum | 25% | Consistent across both years |
| Trend Pullback | 20% | Solid baseline with earnings filter |
| Breakout | 15% | Good in trend years, noisy in flat years |
| Mean Reversion | 5% | Weakest performer, small weight |

A trade only opens when the weighted agreement score reaches **≥ 0.30** (30%). This filters out low-conviction setups where only one or two weak signals agree.

**In plain terms:** No single strategy wins all the time. The ensemble acts like a committee — a trade only happens when enough of the committee agrees. The committee members are weighted by how well they've actually performed historically. Result: fewer trades, higher quality.

**Exit:** 9% stop loss · 4–12% take profit (2.5× ATR) · 6-day time stop

---

## Exit Framework

The six bracket strategies share this exit logic, checked in priority order each 4-hour bar:

1. **Stop Loss** — If the bar's *low* touches the stop level, exit at the stop price (loss capped)
2. **Take Profit** — If the bar's *high* reaches the target, exit at the take-profit price (gain locked)
3. **Time Stop** — After the max holding period, exit at close *only if the position is breakeven or better* (avoids locking in losses; the trade keeps running until SL or TP is hit if still underwater)
4. **End of Data** — Exit at the final bar's close (backtest only)

SMA 50 Cross uses a separate daily lifecycle: a 10% emergency stop has priority, then a confirmed daily cross below exits at the next session. It has no take-profit or time-stop exit.

---

## Technical Indicators Used

| Indicator | What It Measures | Used By |
|-----------|-----------------|---------|
| **SMA(20/50)** | Simple Moving Averages — trend direction and daily price cross | All strategies; SMA 50 Cross uses SMA(50) directly |
| **EMA(10/50)** | Exponential Moving Average — faster trend signals | Regime, Ensemble |
| **RSI(14)** | Relative Strength Index — momentum and overbought/oversold | Six bracket strategies |
| **ATR(14)** | Average True Range — volatility used to size take-profit targets | Six bracket strategies |
| **MACD(12,26,9)** | Moving Average Convergence Divergence — momentum crossover | MACD Momentum, Ensemble |
| **Bollinger Bands(20)** | Price channel around moving average — deviation measure | Mean Reversion |
| **Volume SMA(20)** | Average volume baseline | Breakout, MACD Momentum |

---

## Performance Summary

Backtested on NVDA, AMZN, META, AMD, ARM. Each annual run starts at $1,000,
uses whole-share positions capped at 20% of realized equity, compounds within
the year, and resets to $1,000 for the next year.

| Strategy | 2025 P&L | 2026 P&L | Combined |
|----------|----------|----------|----------|
| Trend Pullback | +$87.46 | +$59.22 | **+$146.68** |
| Breakout | +$78.64 | +$3.95 | +$82.59 |
| Mean Reversion | +$35.10 | -$23.15 | +$11.95 |
| MACD Momentum | +$29.89 | +$3.99 | +$33.88 |
| **Ensemble** | **+$91.83** | **+$243.87** | **+$335.70** |
| **Regime Adaptive** | **+$196.45** | **+$207.28** | **+$403.73** |
| **SMA 50 Cross (daily)** | **+$117.12** | **+$146.19** | **+$263.31** |

*Generated 2026-07-18 from Alpaca SIP bars. The 2026 column is year-to-date
through the latest completed data, not a full calendar year.*

---

## Architecture

```
alpaca_swing_bot_v2_multi_strategy/
├── bot.py                  # Entry point — paper trades one strategy per run
├── strategy.py             # Compatibility shim for all 7 registered strategies
├── strategies/             # One module per strategy + shared exit framework
├── config.py               # Parameters, universe, position sizing
├── backtest_2024.py        # Backtest runner for 2024 data
├── backtest_2025.py        # Backtest runner for 2025 data
├── backtest_2026.py        # Backtest runner for 2026 data
├── dashboard/
│   ├── server.py           # FastAPI app — serves the dashboard
│   ├── db.py               # SQLite read/write for backtest results
│   ├── bot_hooks.py        # Real-time trade event hooks from bot.py
│   └── index.html          # Dashboard UI (KPIs, positions, strategy cards)
├── research/               # Parameter optimisation and experiment tooling
├── logger_setup.py         # Structured logging
├── notifier.py             # Gmail trade notifications
└── requirements.txt
```

---

## Setup

```powershell
# Windows — PowerShell
cd C:\Data\ai_projects\alpaca_swing_bot_v2_multi_strategy
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your credentials:

```
ALPACA_KEY=your_alpaca_key
ALPACA_SECRET=your_alpaca_secret
ALPACA_PAPER=true
GMAIL_USER=you@gmail.com
GMAIL_APP_PASSWORD=your_app_password
NOTIFY_EMAIL=you@gmail.com
```

---

## Usage

```powershell
# 1. Run backtests (populates the database — do this first)
python backtest_2025.py
python backtest_2026.py

# 2. Start the dashboard
python -m uvicorn dashboard.server:app --host 0.0.0.0 --port 8004
# Open: http://localhost:8004

# 3. Run the bot (paper trading)
python bot.py                                    # trend_pullback (default)
python bot.py --strategy ensemble                # recommended
python bot.py --strategy regime
python bot.py --strategy breakout
python bot.py --strategy sma_50_cross              # completed daily candles
python bot.py --strategy momentum_macd
python bot.py --strategy mean_reversion

# Continuous mode — re-runs every 30 minutes
python bot.py --strategy ensemble --loop
python bot.py --strategy ensemble --loop --interval 60

# Parameter optimisation
python -c "from research.optimizer import random_search; from config import StrategyType; r = random_search(StrategyType.TREND_PULLBACK, 2026); print(r[:3])"
```

### Dashboard routes

| Route | Content |
|-------|---------|
| `/` | Home — KPIs, open positions, recent trades, all backtest results |
| `/` → Strategies tab | All 7 strategy cards with entry rules, timeframe, and 3-year P&L |
| `/backtest-2024` | Full Plotly interactive report for 2024 |
| `/backtest-2025` | Full Plotly interactive report for 2025 |
| `/backtest-2026` | Full Plotly interactive report for 2026 |

---

## Research Loop

The bot includes an autoresearch-inspired experimentation framework (see `program.md`):

1. Propose strategy parameter modifications
2. Run backtests on both 2025 and 2026 data
3. Compare results against stored baselines in the SQLite database
4. Keep changes only if they improve performance across **both** years (prevents overfitting to one period)
5. Log every experiment for a full audit trail

---

## Broker & Safety Notes

- All trading uses **Alpaca Paper Trading** (`ALPACA_PAPER=true` is hardcoded in `bot.py`)
- No real money is at risk by default
- To switch to live trading, change `paper=True` to `paper=False` in `bot.py` and ensure your Alpaca account is funded
- The bot does not use margin or short selling — long positions only
