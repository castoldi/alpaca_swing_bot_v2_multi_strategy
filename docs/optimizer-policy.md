# Optimizer execution and evidence policy

Updated 2026-09-21 for F15, version 0.24.20.

## One execution and risk path

`research.optimizer.run_backtest_for_params(params, strategy, year)` delegates to
`backtest_2025.run_strategy_year`. It uses the selected strategy's registered
timeframe and `strategy_universe`: TQQQ Momentum uses its leveraged universe on
4-hour bars; SMA Cross uses daily bars; the other strategies use 4-hour bars.
History comes through the annual loader and the configured feed/adjustment policy.
There is no fallback to daily candles or another provider.

Signal candidates use regular-session minute execution. Annual, optimizer, and
historical multi-year portfolios share `run_configured_portfolio`. Nonempty
candidate sets require valuation data for the daily-loss guard. Each calendar
year starts from the supplied initial equity; cash, position, leveraged-exposure,
daily-loss, and tax settings use the supplied parameters rather than unrelated
global defaults. The lower-level `run_annual_portfolio` retains its explicit
research options for existing callers.

`run_independent_annual_portfolios` now requires the keyword `price_frames`;
callers can also supply `params`. This prevents silently omitting the loss guard.
An empty candidate set can return an empty portfolio without price data.

## Stable inputs during a search

`load_dataset(strategy, year)` reads signal bars and matching minute bars once,
including the preceding session close needed for the first daily-loss baseline.
For earnings-sensitive strategies it also copies the archived earnings history.
Missing historical earnings evidence continues to suppress the affected signals;
current earnings knowledge is not substituted for unavailable past observations.

The dataset keeps private copies and provides fresh copies to each trial. Its
fingerprint covers frame values, indexes, source attributes, strategy/year, and
earnings history. Trials recompute indicators from the same raw inputs using
their own parameters. A supplied dataset must match the strategy and year.

The snapshot is held in memory for the experiment, not permanently archived by
this API. Keep an external copy of the data if future replay after cache revisions
is required; the returned fingerprint identifies inputs but cannot reconstruct
them. Source reads happen sequentially, so freezing does not imply that separate
provider requests were an atomic snapshot of all instruments.

## Effective search parameters

The initial search spaces are deliberately small. They do not claim to search
every strategy parameter. F16's ineffective or disputed rules are excluded.

| Strategy | Fields varied | Bounds |
|---|---|---|
| Trend Pullback | `stop_loss_pct`, `atr_tp_multiple`, `rsi_pullback_max`, `rsi_lookback` | 0.05–0.15; 1.5–4.0; 40–65; 5–20 |
| Breakout | `breakout_stop_loss_pct`, `breakout_atr_multiple` | 0.05–0.15; 1.5–4.0 |
| Mean Reversion | `mr_stop_loss_pct`, `mr_atr_multiple` | 0.03–0.12; 1.0–3.0 |
| MACD Momentum | `macd_stop_loss_pct`, `macd_tp_multiple` | 0.05–0.15; 1.5–4.0 |
| Ensemble | `ensemble_stop_loss_pct`, `ensemble_tp_multiple` | 0.05–0.15; 1.5–4.0 |
| Regime Adaptive | `stop_loss_pct`, `atr_tp_multiple` | 0.05–0.15; 1.5–4.0 |
| SMA Cross | `sma_cross_stop_loss_pct` | 0.05–0.15 |
| TQQQ Momentum | `tqqq_stop_loss_pct` | 0.04–0.12 |

Integer bounds produce integer draws; float draws are rounded to two decimal
places, preserving the search's existing precision. Stop/target floors and caps
can make distinct settings yield identical trades. Every field has a controlled
fixture proving it affects its intended rule, not a promise that every draw
changes trades on every dataset.

## Baselines and trial accounting

```python
from dataclasses import replace
from config import PARAMS, StrategyType
from research.optimizer import load_dataset, run_backtest_for_params, random_search

baseline = replace(PARAMS, position_size_pct=0.15)
dataset = load_dataset(StrategyType.BREAKOUT, 2025)
baseline_trades = run_backtest_for_params(
    baseline, StrategyType.BREAKOUT, 2025, dataset=dataset
)
results, report = random_search(
    StrategyType.BREAKOUT, 2025, iterations=30, seed=42, baseline=baseline
)
print(results[0]['search'])
print(report.summary())
```

The search loads its own snapshot; use recorded fingerprints to establish whether
a separately run baseline used the same inputs. `iterations` counts attempted
random configurations and must be a positive integer. The unchanged baseline is
not silently added as another trial. Every sampled configuration is created by
replacing only the searched fields on `baseline` and selecting the requested
strategy. The search uses a local random generator.

Each result retains full parameters, overrides, configuration identity, duplicate
origin, dataset metadata, statistics, dated returns, and attempted/distinct/repeated
trial counts. Exact repeated configurations reuse computation but remain separate
entries in the multiple-testing panel; losers and repeats are not hidden. Distinct
configurations with identical outcomes are still distinct trials. This preserves
conservative search accounting and the existing `(results, report)` return shape.

Optimizer drawdown uses the supplied starting equity and includes that initial
peak, so a loss on the first trade is visible. It is a realized-equity measure,
not a mark-to-market drawdown estimate.

No search automatically changes live settings, promotes a winner, writes trading
history, or regenerates saved reports. Older optimizer winners and backtest
performance claims require re-evaluation; this correction does not validate
them or establish improved returns. Normal out-of-sample and multiple-testing
requirements still apply.
