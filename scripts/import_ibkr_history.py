"""Import 4h + daily history from IB Gateway into the ``ibkr`` cache partition.

Read-only IBKR connection (the Gateway ``ibkr_trading_bot`` runs), own client
id. Stores into ``cache/market_data.db`` under feed ``ibkr``, alongside the
Alpaca ``sip``/``iex`` partitions. Resumable: a symbol/timeframe that already
covers the requested start is skipped unless ``--force``.

Usage:
    python scripts/import_ibkr_history.py                       # bot universe + SPY, 1999→today
    python scripts/import_ibkr_history.py --tickers NVDA --timeframes 1d
    python scripts/import_ibkr_history.py --force               # re-download everything
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

import ibkr_history  # noqa: E402
from config import LEVERAGED_TICKERS, TICKERS  # noqa: E402
from logger_setup import get_logger  # noqa: E402
from market_cache import CACHE_DB, MarketDataCache  # noqa: E402

log = get_logger("import_ibkr_history")
DEFAULT_START = date(1999, 1, 1)  # a 2000 backtest's indicator warmup starts in 1999


def covered_start(symbol: str, timeframe: str) -> pd.Timestamp | None:
    with closing(sqlite3.connect(str(CACHE_DB))) as con:
        row = con.execute(
            "SELECT requested_start FROM coverage WHERE symbol=? AND timeframe=? "
            "AND feed='ibkr' AND adjustment='all'", (symbol, timeframe),
        ).fetchone()
    return pd.Timestamp(row[0]) if row else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tickers", nargs="+",
                        default=[*TICKERS, *LEVERAGED_TICKERS, "SPY"])
    parser.add_argument("--timeframes", nargs="+", default=["1d", "4h"], choices=["1d", "4h"])
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--force", action="store_true", help="re-download covered series")
    args = parser.parse_args()

    ib = ibkr_history.connect()
    fetcher = ibkr_history.IBKRHistory(ib)
    cache = MarketDataCache(fetcher=fetcher)
    end = datetime.now(timezone.utc).date() + timedelta(days=1)
    failures = []
    try:
        for ticker in args.tickers:
            for timeframe in args.timeframes:
                have = covered_start(ticker, timeframe)
                if have is not None and have <= pd.Timestamp(args.start) and not args.force:
                    log.info("%s %s already imported from %s — skipping", ticker, timeframe, have.date())
                    continue
                try:
                    bars = cache.get_bars(ticker, args.start, end, timeframe, feed="ibkr", refresh=True)
                    first = bars.index.min() if len(bars) else None
                    log.info("IMPORTED %s %s: %d bars %s -> %s", ticker, timeframe, len(bars),
                             first, bars.index.max() if len(bars) else None)
                    print(f"{ticker:5} {timeframe:3} {len(bars):>7} bars  {first} -> "
                          f"{bars.index.max() if len(bars) else None}", flush=True)
                except Exception as exc:  # noqa: BLE001 — keep importing the rest
                    log.exception("FAILED %s %s", ticker, timeframe)
                    failures.append(f"{ticker} {timeframe}: {exc}")
    finally:
        ib.disconnect()
    if failures:
        print("FAILURES:\n  " + "\n  ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
