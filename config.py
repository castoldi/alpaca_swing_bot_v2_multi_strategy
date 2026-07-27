"""Shared configuration for Alpaca Swing Bot V2."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env", override=True)


# --- Strategy type -----------------------------------------------------------
class StrategyType(str, Enum):
    TREND_PULLBACK = "trend_pullback"
    BREAKOUT = "breakout"
    MEAN_REVERSION = "mean_reversion"
    SMA_50_CROSS = "sma_50_cross"   # V2: daily price/SMA(50) cross
    ENSEMBLE = "ensemble"           # V2: ML-weighted signal combiner
    REGIME_ADAPTIVE = "regime"      # V2: market-regime-aware strategy
    MOMENTUM_MACD = "momentum_macd" # V2: MACD + momentum
    TQQQ_MOMENTUM = "tqqq_momentum" # V2: TSI entry / EMA break exit (3x ETF only)


# --- Universe ----------------------------------------------------------------
# Shared universe: every strategy without its own `tickers` override trades these.
TICKERS: list[str] = [
    "NVDA",
    "AMZN",
    "META",
    "AMD",
    "ARM",
]

# Leveraged ETFs are deliberately NOT in TICKERS. A 3x ETF decays through
# whipsaw, so the fixed-bracket exit the other strategies use is the wrong
# shape for it — backtests of those strategies on TQQQ went negative in 2026.
# It is traded only by `tqqq_momentum`, which scopes itself here via
# BaseStrategy.tickers.
LEVERAGED_TICKERS: list[str] = [
    "TQQQ",
]

# Everything the bot may hold — for dashboard snapshots and position display.
ALL_TICKERS: list[str] = TICKERS + LEVERAGED_TICKERS


# --- Bar timeframe -----------------------------------------------------------
# All strategies, backtests, the live bot, and the dashboard charts run on this
# candle timeframe. 4h bars are sourced from Alpaca via data_feed.py — yfinance
# has no native 4h interval and caps intraday history at ~730 days (so 2024 is
# unavailable there). Change here to switch the whole system's timeframe.
BAR_TIMEFRAME = "4h"
# Warmup history fetched before a backtest window so indicators are primed.
HISTORY_WARMUP_DAYS = 90
# Position fraction sold at TP1 / TP2 / TP3 (must sum to 1.0).
TP_SPLITS: tuple[float, float, float] = (0.33, 0.33, 0.34)


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------
GPU_NAME = "NVIDIA GeForce RTX 5050 Laptop GPU"
GPU_VRAM_MB = 4095
IS_WINDOWS = True


# --- Strategy params ---------------------------------------------------------
@dataclass(frozen=True)
class StrategyParams:
    strategy: StrategyType = StrategyType.TREND_PULLBACK

    # ── Position sizing ───────────────────────────────────────────────────
    initial_backtest_equity: float = 1000.0
    position_size_pct: float = 0.20
    max_concurrent_positions: int = 5

    # ── Live risk guards ──────────────────────────────────────────────────
    # Skip an entry when the live price has drifted further than this from the
    # signal bar close: the SL/TP geometry would no longer match the backtest.
    entry_max_slippage_pct: float = 0.015
    # Kill switch: once the account is down this much vs yesterday's close
    # equity, new entries are disabled (exits and broker-held protection keep
    # working). Re-evaluated fresh every cycle, not latched for the rest of
    # the day: if equity recovers back above the threshold later the same
    # session, new entries resume without waiting for the next calendar day.
    max_daily_loss_pct: float = 0.03
    # Total notional allowed across LEVERAGED_TICKERS, as a fraction of equity.
    #
    # max_concurrent_positions (5) x position_size_pct (20%) = 100% of equity,
    # and that limit is count-based, so it cannot see correlation. Leveraged
    # ETFs move together and their entry signals fire together, so without this
    # cap a multi-ETF leveraged universe could put the whole account into 3x
    # instruments at once — roughly 3x account beta, cash-funded, with no margin
    # call to stop it.
    #
    # Default 0.20 equals exactly one 20% position, which is the exposure a
    # single-ticker leveraged universe already has: adding a second leveraged
    # ticker therefore cannot raise risk until this number is deliberately
    # raised. Enforced identically in live sizing and in run_annual_portfolio.
    max_leveraged_exposure_pct: float = 0.20

    # ── Tax awareness ─────────────────────────────────────────────────────
    # Rates used only to forecast a liability on the dashboard; they change no
    # trading behaviour. Defaults are a common federal bracket plus the 15%
    # long-term rate — set them to your own marginal rates.
    tax_short_term_rate: float = 0.24
    tax_long_term_rate: float = 0.15
    # Year-end wash-sale guard. Intra-year wash sales are harmless in aggregate
    # (the disallowed loss rides in the replacement lot's basis and is recovered
    # on the next sale), so the guard runs only from `tax_guard_start_*` through
    # 31 December, where a new wash sale would push a deduction into next year.
    # Turning this off never breaks a rule — it only forfeits the deferral.
    tax_year_end_guard: bool = True
    tax_guard_start_month: int = 12
    tax_guard_start_day: int = 1
    # Conservative alternative to the surgical guard above: refuse ALL entries
    # for a flat window spanning 31 December, which is the standard way an
    # active trader sidesteps §1091 entirely. Off by default because it costs a
    # month of trading a year; `tax_hard_block_days` is centred on 31 December,
    # so 31 days runs ~16 Dec to ~15 Jan.
    tax_hard_block: bool = False
    tax_hard_block_days: int = 31

    # §475(f) mark-to-market election. When elected, trading is EXEMPT from the
    # wash-sale rule and from the capital-loss limitation, and gains/losses
    # become ordinary income. Defaults off: the election is irrevocable without
    # IRS consent, requires qualifying for Trader Tax Status, and is a decision
    # for the account owner and a CPA — never for the bot.
    tax_mtm_475f: bool = False

    # Wash-sale matching is EXACT SYMBOL by default. Each tuple here declares a
    # set of tickers to treat as substantially identical to one another — e.g.
    # two share classes, or a fund and its own successor. Deliberately empty:
    # whether a leveraged ETF is substantially identical to its underlying index
    # fund (TQQQ vs QQQ) is genuinely unsettled, and the bot should not assert a
    # position on it.
    tax_identical_groups: tuple[tuple[str, ...], ...] = ()

    # §1091 applies to "stocks or securities". Digital assets are property, so
    # the wash-sale rule does not currently reach them; gains/losses are still
    # tracked and taxed. Legislation to extend §1091 to digital assets has been
    # proposed repeatedly — revisit before relying on this.
    tax_crypto_symbols: tuple[str, ...] = ()

    # Lot relief method for partial sales. FIFO is the IRS default when no
    # specific identification is made at the time of sale.
    tax_lot_method: str = "fifo"        # fifo | lifo | specific

    # Progressive brackets, NIIT and quarterly estimates. Off by default so the
    # dashboard keeps using the two flat rates above and existing figures do not
    # move; turn on for a materially better estimate.
    tax_use_brackets: bool = False
    tax_filing_status: str = "single"   # single | married_joint
    tax_other_income: float = 0.0       # wages etc., sets the starting bracket
    tax_niit: bool = True               # 3.8% net investment income tax
    tax_estimated_payments: bool = True  # quarterly safe-harbour schedule

    # ── Shared data window ────────────────────────────────────────────
    history_days: int = 90
    one_position_per_ticker: bool = True

    # ── V1 Indicators ───────────────────────────────────────────────────
    sma_fast: int = 20
    sma_slow: int = 50
    rsi_period: int = 14
    rsi_pullback_max: float = 55.0
    rsi_lookback: int = 10

    # ── Shared risk ───────────────────────────────────────────────────────
    stop_loss_pct: float = 0.10
    atr_period: int = 14
    atr_tp_multiple: float = 2.0
    take_profit_cap_pct: float = 0.08
    take_profit_floor_pct: float = 0.03
    max_holding_days: int = 5  # time-stop: exit only if breakeven or better

    # ── Breakout ─────────────────────────────────────────────────────────
    breakout_lookback: int = 20
    breakout_volume_mult: float = 1.5
    breakout_atr_multiple: float = 3.0
    breakout_tp_cap_pct: float = 0.15
    breakout_tp_floor_pct: float = 0.05
    breakout_max_holding_days: int = 7
    breakout_stop_loss_pct: float = 0.08
    breakout_range_lookback: int = 10
    breakout_range_mult: float = 1.3

    # ── Mean Reversion ────────────────────────────────────────────────────
    mr_rsi_oversold: float = 50.0
    mr_rsi_lookback: int = 7
    mr_deviation_pct: float = 0.005
    mr_sma_fast: int = 20
    mr_atr_multiple: float = 1.5
    mr_tp_cap_pct: float = 0.05
    mr_tp_floor_pct: float = 0.015
    mr_max_holding_days: int = 3
    mr_stop_loss_pct: float = 0.07
    mr_bollinger_mult: float = 2.2

    # ── V2: MACD Momentum ────────────────────────────────────────────────
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    macd_stop_loss_pct: float = 0.09
    macd_tp_multiple: float = 2.5
    macd_tp_cap_pct: float = 0.12
    macd_tp_floor_pct: float = 0.04
    macd_max_holding_days: int = 6

    # ── V2: Ensemble ─────────────────────────────────────────────────────
    ensemble_min_votes: int = 2      # min strategies that must agree
    ensemble_stop_loss_pct: float = 0.09
    ensemble_tp_multiple: float = 2.5
    ensemble_tp_cap_pct: float = 0.12
    ensemble_tp_floor_pct: float = 0.04
    ensemble_max_holding_days: int = 6

    # ── V2: Regime Adaptive ──────────────────────────────────────────────
    regime_lookback: int = 21
    regime_sma_short: int = 10
    regime_sma_long: int = 50
    regime_vix_period: int = 14
    regime_risk_on_mult: float = 1.2
    regime_risk_off_mult: float = 0.7

    # ── Earnings avoidance filter ─────────────────────────────────────────
    earnings_avoid_days: int = 3  # skip entries N trading days before earnings

    # ── V2: Daily SMA 50 Cross ───────────────────────────────────────────
    sma_cross_period: int = 50
    sma_cross_stop_loss_pct: float = 0.10

    # ── V2: TQQQ Momentum (leveraged ETF, 4h) ────────────────────────────
    # TSI(25,13) crossing its 13-period signal line is the entry; the exit is
    # a close below EMA(50). Backtests 2022-2026 (Alpaca SIP 4h): positive
    # every year including 2022, when TQQQ buy-and-hold was -79.5%.
    # No take-profit — the edge comes from riding trends until they break,
    # not from a fixed target. The stop is gap insurance and rarely binds.
    tqqq_tsi_long: int = 25
    tqqq_tsi_short: int = 13
    tqqq_tsi_signal: int = 13
    tqqq_ema_period: int = 50
    tqqq_stop_loss_pct: float = 0.08


PARAMS = StrategyParams()

# Strategies that are active. Remove a name to disable it in the bot and backtests.
ENABLED_STRATEGIES: set[str] = {
    "trend_pullback",
    "breakout",
    "mean_reversion",
    "momentum_macd",
    "regime",
    "sma_50_cross",
    "ensemble",
    "tqqq_momentum",
}


# --- Alpaca credentials ------------------------------------------------------
ALPACA_KEY: str | None = os.getenv("ALPACA_KEY")
ALPACA_SECRET: str | None = os.getenv("ALPACA_SECRET")
ALPACA_PAPER: bool = os.getenv("ALPACA_PAPER", "true").lower() in {"1", "true", "yes"}


# --- Gmail notifier ----------------------------------------------------------
GMAIL_USER: str | None = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD: str | None = os.getenv("GMAIL_APP_PASSWORD")
NOTIFY_EMAIL: str | None = os.getenv("NOTIFY_EMAIL") or GMAIL_USER


# --- Output paths ------------------------------------------------------------
REPORTS_DIR = ROOT / "reports"
REPORTS_DIR.mkdir(exist_ok=True)
DASHBOARD_PATH = REPORTS_DIR / "backtest_dashboard.html"
