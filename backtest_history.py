"""Cumulative Alpaca SIP backtest from 2016 through completed market data."""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backtest_2025 import (
    _MARKET_CACHE,
    apply_portfolio_cap,
    compute_max_drawdown,
    compute_stats,
    download_history,
)
from build_report_2025 import build_report_2025
from config import PARAMS, TICKERS
from logger_setup import get_logger
from strategies import REGISTRY, Trade, backtest_ticker, get_enabled


log = get_logger(__name__)
ROOT = Path(__file__).parent
REPORTS_DIR = ROOT / "reports"
OUTPUT_HTML = REPORTS_DIR / "backtest_2016_present.html"
OUTPUT_JSON = REPORTS_DIR / "backtest_2016_present.json"
EARLIEST_HISTORY = date(2016, 1, 1)


def validate_range(start: date, end: date) -> None:
    """Reject ranges outside Alpaca's supported equity-history boundary."""
    if start < EARLIEST_HISTORY:
        raise ValueError(
            f"Historical backtests cannot start before {EARLIEST_HISTORY.isoformat()}"
        )
    if end < start:
        raise ValueError("Backtest end date cannot be before start date")


def _stats_with_risk(trades: list[Trade]) -> dict[str, Any]:
    stats = compute_stats(trades)
    stats["max_drawdown_pct"] = compute_max_drawdown(trades)
    stats["roi_on_cap"] = (
        stats["total_pnl"] / PARAMS.max_concurrent_capital
        if PARAMS.max_concurrent_capital
        else 0.0
    )
    return stats


def yearly_stats(
    trades: list[Trade], start_year: int, end_year: int
) -> dict[int, dict[str, Any]]:
    """Summarize accepted trades by the year their P&L was realized."""
    result: dict[int, dict[str, Any]] = {}
    for year in range(start_year, end_year + 1):
        realized = [
            trade for trade in trades if pd.Timestamp(trade.exit_date).year == year
        ]
        result[year] = _stats_with_risk(realized)
    return result


def _json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, np.generic):
        return _json_ready(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return pd.Timestamp(value).isoformat()
    return value


def _report_label(start: date, end: date) -> str:
    if start == EARLIEST_HISTORY and end >= date.today():
        return f"{start.year}–Present Backtest"
    return f"{start.isoformat()}–{end.isoformat()} Backtest"


def _select_strategies(strategy_name: str | None) -> list:
    if strategy_name is None:
        return get_enabled()
    if strategy_name not in REGISTRY:
        choices = ", ".join(REGISTRY)
        raise ValueError(f"Unknown strategy '{strategy_name}'. Available: {choices}")
    return [REGISTRY[strategy_name]]


def _print_summary(payload: dict[str, Any]) -> None:
    print("\nCumulative strategy results")
    print(f"{'Strategy':<20} {'Trades':>7} {'Win %':>8} {'P&L':>12} {'Max DD':>9}")
    for name, data in sorted(
        payload["strategies"].items(),
        key=lambda item: item[1]["cumulative"]["total_pnl"],
        reverse=True,
    ):
        stats = data["cumulative"]
        print(
            f"{name:<20} {stats['trades']:>7} {stats['win_rate'] * 100:>7.1f}% "
            f"${stats['total_pnl']:>+10.2f} {stats['max_drawdown_pct'] * 100:>8.1f}%"
        )

    print("\nYearly P&L")
    years = range(
        int(payload["requested_start"][:4]),
        int(payload["requested_end"][:4]) + 1,
    )
    header = "Strategy".ljust(20) + "".join(f"{year:>11}" for year in years)
    print(header)
    for name, data in payload["strategies"].items():
        cells = "".join(
            f"${data['yearly'][str(year)]['total_pnl']:>+9.2f}" for year in years
        )
        print(f"{name:<20}{cells}")


def run_history_backtest(
    start: date = EARLIEST_HISTORY,
    end: date | None = None,
    strategy_name: str | None = None,
) -> dict[str, Any]:
    """Run strategies over one cached range and write HTML plus JSON results."""
    requested_end = end or date.today()
    validate_range(start, requested_end)
    strategies_to_run = _select_strategies(strategy_name)
    timeframes = sorted({strategy.timeframe for strategy in strategies_to_run})

    ticker_data: dict[tuple[str, str], pd.DataFrame] = {}
    first_bars: list[pd.Timestamp] = []
    last_bars: list[pd.Timestamp] = []
    for timeframe in timeframes:
        for ticker in TICKERS:
            log.info("Loading %s (%s) from persistent SIP cache...", ticker, timeframe)
            frame = download_history(ticker, start, requested_end, timeframe)
            if frame.empty or len(frame) < PARAMS.sma_slow + 5:
                log.warning(
                    "%s (%s): insufficient available history — skipping",
                    ticker,
                    timeframe,
                )
                frame = pd.DataFrame()
            else:
                first_bars.append(pd.Timestamp(frame.index.min()))
                last_bars.append(pd.Timestamp(frame.index.max()))
            ticker_data[(ticker, timeframe)] = frame

    strategy_results: dict[str, dict[str, Any]] = {}
    per_strategy_details: dict[str, dict] = {}
    payload_strategies: dict[str, dict[str, Any]] = {}
    overall_best: str | None = None
    best_pnl = float("-inf")

    for strategy in strategies_to_run:
        candidates: list[Trade] = []
        per_ticker: dict[str, tuple[pd.DataFrame, list[Trade]]] = {}
        for ticker in TICKERS:
            frame = ticker_data[(ticker, strategy.timeframe)]
            if frame.empty:
                continue
            trades = backtest_ticker(
                frame,
                ticker,
                pd.Timestamp(start),
                PARAMS,
                strategy,
            )
            candidates.extend(trades)
            per_ticker[ticker] = (frame, trades)

        accepted, skipped = apply_portfolio_cap(
            candidates,
            PARAMS.dollars_per_trade,
            PARAMS.max_concurrent_capital,
        )
        stats = _stats_with_risk(accepted)
        stats["_trades"] = accepted
        stats["skipped"] = skipped
        strategy_results[strategy.name] = stats
        per_strategy_details[strategy.name] = per_ticker

        cumulative = {key: value for key, value in stats.items() if key != "_trades"}
        annual = yearly_stats(accepted, start.year, requested_end.year)
        payload_strategies[strategy.name] = {
            "timeframe": strategy.timeframe,
            "cumulative": _json_ready(cumulative),
            "yearly": _json_ready(annual),
        }

        if stats["total_pnl"] > best_pnl:
            best_pnl = stats["total_pnl"]
            overall_best = strategy.name

        log.info(
            "%s: %d trades, WR %.1f%%, P&L %+.2f",
            strategy.name,
            stats["trades"],
            stats["win_rate"] * 100,
            stats["total_pnl"],
        )

    payload: dict[str, Any] = {
        "requested_start": start.isoformat(),
        "requested_end": requested_end.isoformat(),
        "actual_start": min(first_bars).isoformat() if first_bars else None,
        "actual_end": max(last_bars).isoformat() if last_bars else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_source": "Alpaca SIP historical data (adjustment=all)",
        "tickers": list(TICKERS),
        "strategies": payload_strategies,
        "cache": _json_ready(_MARKET_CACHE.status()),
    }

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    report = build_report_2025(
        strategy_results,
        per_strategy_details,
        overall_best,
        report_label=_report_label(start, requested_end),
        data_source="Alpaca SIP historical data",
    )
    OUTPUT_HTML.write_text(report, encoding="utf-8")
    OUTPUT_JSON.write_text(
        json.dumps(_json_ready(payload), indent=2, allow_nan=False),
        encoding="utf-8",
    )
    _print_summary(payload)
    print(f"\nHTML report: {OUTPUT_HTML}")
    print(f"JSON summary: {OUTPUT_JSON}")
    return payload


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid date '{value}'; expected YYYY-MM-DD"
        ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run all strategies over cached Alpaca SIP history"
    )
    parser.add_argument("--start", type=_parse_date, default=EARLIEST_HISTORY)
    parser.add_argument("--end", type=_parse_date, default=date.today())
    parser.add_argument("--strategy", choices=list(REGISTRY))
    args = parser.parse_args()
    try:
        run_history_backtest(args.start, args.end, args.strategy)
    except Exception as exc:
        log.exception("Historical backtest failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
