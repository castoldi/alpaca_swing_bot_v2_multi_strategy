"""Generate real, strategy-accurate trade examples for the Strategies page.

For each strategy we scan ~18 months of daily bars across the universe, run the
strategy's own entry checker to find its most recent real signals, and simulate
how each trade resolved. The result is a small candle window plus the actual
entry / stop-loss / take-profit levels and the exit — everything the front end
needs to draw an annotated candlestick that *visualises the strategy*.

Results are cached (in-process + on disk) because the yfinance fetch + full scan
takes a few seconds; the examples only change as new bars arrive.
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from config import PARAMS, TICKERS, BAR_TIMEFRAME
from strategy import add_indicators, simulate_exit
from strategies import get_all
from strategies.base import backtest_signal_exit_ticker
from logger_setup import get_logger
import data_feed

log = get_logger(__name__)

_CACHE_FILE = Path(__file__).resolve().parent.parent / "reports" / "strategy_examples_cache.json"
_TTL_SECONDS = 6 * 3600          # refresh examples at most every 6 hours
_HISTORY_DAYS = 540              # ~18 months, enough to surface ≥2 signals per strategy
_WARMUP = 60                     # indicators/ensemble need 60 bars before first signal
_BARS_BEFORE = 15                # candles to show before the entry
_BARS_AFTER = 8                  # candles to show after the exit
_MAX_EXAMPLES = 2

_mem: dict = {"ts": 0.0, "data": None}


# ── data ──────────────────────────────────────────────────────────────────────

def _fetch_bars(
    ticker: str, timeframe: str, days: int = _HISTORY_DAYS
) -> pd.DataFrame:
    """Recent strategy-timeframe bars for building chart examples."""
    df = data_feed.fetch_recent(ticker, days=days, timeframe=timeframe)
    return data_feed.completed_bars(df, timeframe)


def _target_value(strategy, value: float):
    return round(float(value), 2) if strategy.has_take_profit else None


def _build_example(ticker: str, idx: int, sig, df: pd.DataFrame, strategy) -> dict | None:
    entry_idx = idx
    entry_price = float(sig.entry_price)
    stop_loss = float(sig.stop_loss)
    take_profit = float(sig.take_profit)

    if strategy.exit_mode == "signal_with_stop":
        trades = backtest_signal_exit_ticker(
            df, ticker, pd.Timestamp(sig.date), PARAMS, strategy
        )
        if not trades:
            return None
        trade = trades[0]
        entry_idx = df.index.get_loc(trade.entry_date)
        entry_price = float(trade.entry_price)
        stop_loss = float(trade.stop_loss)
        take_profit = float(trade.take_profit)
        exit_date = trade.exit_date
        exit_price = trade.exit_price
        reason = trade.exit_reason
        bars_held = trade.bars_held
    else:
        exit_date, exit_price, reason, bars_held = simulate_exit(
            df, idx, sig, PARAMS
        )

    try:
        exit_pos = df.index.get_loc(exit_date)
    except Exception:
        exit_pos = min(entry_idx + bars_held, len(df) - 1)

    start = max(0, entry_idx - _BARS_BEFORE)
    end = min(len(df), max(exit_pos, entry_idx) + _BARS_AFTER + 1)
    win = df.iloc[start:end]

    bars = [{
        "t": d.strftime("%Y-%m-%d"),
        "o": round(float(r["open"]), 2),
        "h": round(float(r["high"]), 2),
        "l": round(float(r["low"]), 2),
        "c": round(float(r["close"]), 2),
    } for d, r in win.iterrows()]

    pnl_pct = round((float(exit_price) / entry_price - 1.0) * 100, 2) if entry_price else 0.0
    return {
        "ticker": ticker,
        "bars": bars,
        "entry": round(entry_price, 2),
        "sl": round(stop_loss, 2),
        "tp": _target_value(strategy, take_profit),
        "entryDate": df.index[entry_idx].strftime("%Y-%m-%d"),
        "exitDate": pd.Timestamp(exit_date).strftime("%Y-%m-%d"),
        "exitPrice": round(float(exit_price), 2),
        "exitReason": reason,
        "barsHeld": int(bars_held),
        "outcome": "win" if float(exit_price) >= entry_price else "loss",
        "pnlPct": pnl_pct,
    }


def _examples_for_strategy(strategy, frames: dict[str, pd.DataFrame]) -> list[dict]:
    checker = strategy.check_entry

    # Collect every signal across the universe, newest first.
    hits: list[tuple[pd.Timestamp, str, int, object]] = []
    for ticker, df in frames.items():
        n = len(df)
        for idx in range(_WARMUP, n):
            try:
                sig = checker(df, idx, PARAMS)
            except Exception:
                sig = None
            if sig is not None:
                hits.append((df.index[idx], ticker, idx, sig))
    hits.sort(key=lambda h: h[0], reverse=True)
    hits = hits[:80]  # only ever need the most recent handful

    built = []
    for _date, ticker, idx, sig in hits:
        example = _build_example(ticker, idx, sig, frames[ticker], strategy)
        if example is not None:
            built.append((ticker, idx, example))

    picked: list[dict] = []
    chosen: list[tuple[str, int]] = []  # (ticker, idx) already used

    def _too_close(ticker: str, idx: int) -> bool:
        # skip near-duplicate signals (same ticker within ~10 bars)
        return any(t == ticker and abs(idx - pi) < 10 for t, pi in chosen)

    def _consider(pred) -> None:
        for ticker, idx, ex in built:
            if len(picked) >= _MAX_EXAMPLES:
                return
            if _too_close(ticker, idx) or not pred(ticker, ex):
                continue
            chosen.append((ticker, idx))
            picked.append(ex)

    used = lambda: {t for t, _ in chosen}
    # Prefer *resolved* trades (a real SL/TP/time-stop outcome) and ticker variety,
    # then relax: resolved-any-ticker → unresolved-distinct → anything.
    _consider(lambda t, ex: ex["exitReason"] != "end_of_data" and t not in used())
    _consider(lambda t, ex: ex["exitReason"] != "end_of_data")
    _consider(lambda t, ex: t not in used())
    _consider(lambda t, ex: True)
    return picked


# ── public API ────────────────────────────────────────────────────────────────

def _compute() -> dict:
    strategies = get_all()
    timeframes = sorted({strategy.timeframe for strategy in strategies})
    frames_by_timeframe: dict[str, dict[str, pd.DataFrame]] = {}
    universe: set[str] = set()
    for timeframe in timeframes:
        frames: dict[str, pd.DataFrame] = {}
        for ticker in TICKERS:
            try:
                df = _fetch_bars(ticker, timeframe)
                if df.empty or len(df) < _WARMUP + 5:
                    continue
                frames[ticker] = add_indicators(df, PARAMS)
                universe.add(ticker)
            except Exception as e:
                log.warning("strategy_examples: failed to fetch %s (%s): %s",
                            ticker, timeframe, e)
        frames_by_timeframe[timeframe] = frames

    examples: dict[str, list[dict]] = {}
    for strategy in strategies:
        try:
            frames = frames_by_timeframe.get(strategy.timeframe, {})
            examples[strategy.name] = _examples_for_strategy(strategy, frames)
        except Exception as e:
            log.error("strategy_examples: failed for %s: %s", strategy.name, e)
            examples[strategy.name] = []

    return {
        "generated_at": pd.Timestamp.now("UTC").isoformat(),
        "timeframe": BAR_TIMEFRAME,
        "timeframes": {strategy.name: strategy.timeframe for strategy in strategies},
        "universe": sorted(universe),
        "examples": examples,
    }


def _load_disk_cache() -> dict | None:
    try:
        if _CACHE_FILE.exists() and (time.time() - _CACHE_FILE.stat().st_mtime) < _TTL_SECONDS:
            data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            expected = {strategy.name for strategy in get_all()}
            if set(data.get("examples", {})) == expected and data.get("timeframes"):
                return data
    except Exception as e:
        log.debug("strategy_examples: disk cache read failed: %s", e)
    return None


def _save_disk_cache(data: dict) -> None:
    try:
        _CACHE_FILE.parent.mkdir(exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception as e:
        log.debug("strategy_examples: disk cache write failed: %s", e)


def get_examples(force: bool = False) -> dict:
    """Return cached per-strategy examples, recomputing if the cache is stale."""
    now = time.time()
    if not force and _mem["data"] is not None and (now - _mem["ts"]) < _TTL_SECONDS:
        return _mem["data"]

    if not force:
        disk = _load_disk_cache()
        if disk is not None:
            _mem.update(ts=now, data=disk)
            return disk

    data = _compute()
    _mem.update(ts=now, data=data)
    _save_disk_cache(data)
    return data


if __name__ == "__main__":  # quick manual check
    out = get_examples(force=True)
    for k, v in out["examples"].items():
        print(f"{k}: {len(v)} example(s)", [e["ticker"] for e in v])
