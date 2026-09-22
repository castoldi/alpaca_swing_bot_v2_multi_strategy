"""Import historical earnings dates as labelled point-in-time approximations.

The live archive (`cache/earnings.db`) only starts on 2026-09-14, so every
earlier backtest suppressed Trend Pullback and the Ensemble's trend vote (V06
in docs/code-review-2026-09-21-verification.md). This fills the history before
the first live observation with weekly synthetic snapshots built from Yahoo's
past earnings dates.

**Approximation (recorded in every row's ``source``):** an earnings date is
assumed public ``LEAD_DAYS`` calendar days before the event. Companies
normally confirm dates two to four weeks ahead, and the filter only needs the
date ``earnings_avoid_days`` sessions ahead. Weeks not bracketed by a
continuous quarterly history (before the first known event, or inside a gap
longer than ``MAX_GAP_DAYS``) stay unknown, so those signals remain
suppressed exactly as before.

Usage:
    python scripts/import_earnings_history.py              # config.TICKERS, before the live archive
    python scripts/import_earnings_history.py --tickers NVDA META
    python scripts/import_earnings_history.py --force      # replace a previous import
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from config import TICKERS  # noqa: E402
from earnings_calendar import CalendarStore, fetch_events, utc  # noqa: E402
from logger_setup import get_logger  # noqa: E402

log = get_logger(__name__)

LEAD_DAYS = 14
CADENCE = pd.Timedelta(days=7)
MAX_GAP_DAYS = 120
SOURCE = (f"historical_import:yfinance past earnings dates; "
          f"approximation: date assumed public >= {LEAD_DAYS} days ahead")


def _event_time(event: str) -> pd.Timestamp:
    ts = pd.Timestamp(event)
    return utc(ts.tz_localize("America/New_York") if ts.tzinfo is None else ts)


def build_snapshots(events, start, end, *, lead_days=LEAD_DAYS, cadence=CADENCE,
                    max_gap_days=MAX_GAP_DAYS):
    """Weekly (observed_at, valid_until, events) tuples covering [start, end).

    A week is certified only when a known event lies within ``max_gap_days``
    both before and after it, i.e. inside a continuous quarterly history.
    """
    timed = sorted((_event_time(e), str(e)) for e in events)
    if not timed:
        return []
    times = [t for t, _ in timed]
    gap = pd.Timedelta(days=max_gap_days)
    lead = pd.Timedelta(days=lead_days)
    out = []
    observed = utc(start)
    end = utc(end)
    while observed < end:
        until = min(observed + cadence, end)
        prior = [t for t in times if t <= observed]
        later = [t for t in times if t > observed]
        if prior and later and observed - prior[-1] <= gap and later[0] - observed <= gap:
            known = [e for t, e in timed if observed - cadence * 2 <= t <= observed + lead]
            out.append((observed, until, known))
        observed = until
    return out


def import_ticker(store, ticker, *, end=None, force=False, fetcher=None):
    fetcher = fetcher or (lambda symbol: fetch_events(symbol, limit=100))
    if store.first_observed(ticker, SOURCE) is not None:
        if not force:
            log.info("%s: historical import already present (use --force)", ticker)
            return 0
        store.delete_source(ticker, SOURCE)
    live_start = store.first_observed(ticker)
    end = utc(end) if end is not None else (live_start or pd.Timestamp.now(tz="UTC"))
    events = fetcher(ticker)
    if not events:
        log.warning("%s: no earnings history returned", ticker)
        return 0
    start = min(_event_time(e) for e in events).normalize()
    rows = build_snapshots(events, start, end)
    for observed, until, known in rows:
        store.record(ticker, observed, known, valid_until=until, source=SOURCE, status="ok")
    log.info("%s: %d weekly snapshots %s -> %s from %d events", ticker, len(rows),
             rows[0][0].date() if rows else "-", rows[-1][1].date() if rows else "-", len(events))
    return len(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tickers", nargs="+", default=list(TICKERS))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    store = CalendarStore()
    total = 0
    for ticker in args.tickers:
        try:
            total += import_ticker(store, ticker, force=args.force)
        except Exception as exc:
            log.error("%s: import failed (%s)", ticker, exc)
    log.info("Imported %d snapshots", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
