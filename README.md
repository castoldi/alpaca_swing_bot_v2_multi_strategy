# Alpaca Swing Bot V2 — Multi-Strategy

An automated swing trading bot that trades a focused universe of high-momentum US equities using six independently-tuned strategies. All execution is through the Alpaca brokerage API (paper trading by default) with a live FastAPI dashboard.

---

## What Is Swing Trading?

Swing trading sits between day trading and long-term investing. Positions are held for **2–7 trading days**, aiming to capture short-term price "swings" — the natural ebb and flow of a stock's price over days rather than hours or months.

Each trade has three pre-defined levels set at entry:

| Level | Purpose |
|-------|---------|
| **Entry price** | The price at which the position opens |
| **Stop loss** | The maximum loss accepted — trade exits automatically if price falls here |
| **Take profit** | The target where the position is closed for a gain |

This means every trade's risk and reward are locked in the moment it opens — there is no holding and hoping.

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
| Capital per trade | $200 |
| Max concurrent capital | $1,000 (5 trades max) |
| Default stop loss | 7–10% below entry (strategy-dependent) |
| Default take profit | 3–12% above entry (capped by ATR) |
| Max holding period | 3–7 days (strategy-dependent) |

The take-profit target is **dynamically sized** using the Average True Range (ATR) — a measure of how much a stock typically moves each day. If the target is too far away to be hit within ~4 days based on recent daily ranges, the trade is skipped entirely (TP reachability filter).

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

### 6. Ensemble (V2) — Recommended

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

## Exit Framework (All Strategies)

All strategies share the same exit logic, checked in priority order each bar:

1. **Stop Loss** — If the bar's *low* touches the stop level, exit at the stop price (loss capped)
2. **Take Profit** — If the bar's *high* reaches the target, exit at the take-profit price (gain locked)
3. **Time Stop** — After the max holding period, exit at close *only if the position is breakeven or better* (avoids locking in losses; the trade keeps running until SL or TP is hit if still underwater)
4. **End of Data** — Exit at the final bar's close (backtest only)

---

## Technical Indicators Used

| Indicator | What It Measures | Used By |
|-----------|-----------------|---------|
| **SMA(20/50)** | 20-day and 50-day Simple Moving Average — trend direction | All strategies |
| **EMA(10/50)** | Exponential Moving Average — faster trend signals | Regime, Ensemble |
| **RSI(14)** | Relative Strength Index — momentum and overbought/oversold | All strategies |
| **ATR(14)** | Average True Range — daily volatility, used to size take-profit targets | All strategies |
| **MACD(12,26,9)** | Moving Average Convergence Divergence — momentum crossover | MACD Momentum, Ensemble |
| **Bollinger Bands(20)** | Price channel around moving average — deviation measure | Mean Reversion |
| **Volume SMA(20)** | Average volume baseline | Breakout, MACD Momentum |

---

## Performance Summary

Backtested on NVDA, AMZN, META, AMD, ARM. $200 per trade, max $1,000 deployed at once.

| Strategy | 2025 P&L | 2026 P&L | Combined |
|----------|----------|----------|----------|
| Trend Pullback | +$71.37 | +$142.02 | **+$213.39** |
| Breakout | -$26.51 | +$117.66 | +$91.15 |
| Mean Reversion | -$26.31 | +$26.68 | +$0.37 |
| MACD Momentum | +$13.31 | +$35.99 | +$49.30 |
| **Ensemble** | **+$199.92** | **+$331.78** | **+$531.70** |
| Regime Adaptive | +$85.69 | +$239.86 | **+$325.55** |

*Results as of 2026-06-02. Run the backtest scripts to regenerate with latest data.*

---

## Architecture

```
alpaca_swing_bot_v2_multi_strategy/
├── bot.py                  # Entry point — paper trades one strategy per run
├── strategy.py             # All 6 strategy engines + shared exit framework
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
| `/` → Strategies tab | All 6 strategy cards with entry rules and 3-year P&L |
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
