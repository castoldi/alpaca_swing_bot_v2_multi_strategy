"""V06: historical earnings import as labelled weekly snapshots."""
import pandas as pd

from config import PARAMS
from earnings_calendar import CalendarStore, apply_filter
from scripts import import_earnings_history as imp

QUARTERLY = ["2025-02-26T16:20-05:00", "2025-05-28T16:20-04:00",
             "2025-08-27T16:20-04:00", "2025-11-19T16:20-05:00",
             "2026-02-25T16:20-05:00"]


def test_weeks_inside_quarterly_history_are_certified_and_edges_stay_unknown():
    rows = imp.build_snapshots(QUARTERLY, "2025-01-01", "2026-03-31")
    observed = [r[0] for r in rows]
    assert observed[0] >= pd.Timestamp("2025-02-26", tz="UTC")      # nothing before first event
    assert observed[-1] < pd.Timestamp("2026-02-25T21:20Z")          # nothing after the last event
    assert all(until > obs for obs, until, _ in rows)


def test_gap_longer_than_limit_is_not_certified():
    events = ["2025-02-26T16:20-05:00", "2025-11-19T16:20-05:00"]   # missing two quarters
    assert imp.build_snapshots(events, "2025-01-01", "2025-12-31") == []


def test_imported_history_blocks_before_earnings_and_clears_otherwise(tmp_path):
    store = CalendarStore(tmp_path / "e.db", fetcher=lambda _: [])
    count = imp.import_ticker(store, "NVDA", end="2026-03-31",
                              fetcher=lambda _: QUARTERLY)
    assert count > 0
    index = pd.DatetimeIndex(["2025-08-26 16:00", "2025-07-15 16:00"])
    frame = pd.DataFrame({"close": [1.0, 1.0]}, index=index)
    decisions = [pd.Timestamp("2025-08-26T20:00Z"), pd.Timestamp("2025-07-15T20:00Z")]
    out = apply_filter(frame, "NVDA", PARAMS, decision_times=decisions, store=store)
    assert out.earnings_status.tolist() == ["blocked", "clear"]


def test_import_is_idempotent_and_force_replaces_only_its_rows(tmp_path):
    store = CalendarStore(tmp_path / "e.db", fetcher=lambda _: [])
    store.record("NVDA", "2026-09-14T13:00Z", ["2026-11-17T16:00-05:00"],
                 source="yfinance current schedule")
    first = imp.import_ticker(store, "NVDA", fetcher=lambda _: QUARTERLY)
    assert imp.import_ticker(store, "NVDA", fetcher=lambda _: QUARTERLY) == 0
    assert imp.import_ticker(store, "NVDA", force=True, fetcher=lambda _: QUARTERLY) == first
    live = [r for r in store.history("NVDA", "2027-01-01") if r["source"] == "yfinance current schedule"]
    assert len(live) == 1
    # Import stops where live observations begin.
    imported = [r for r in store.history("NVDA", "2027-01-01") if r["source"] == imp.SOURCE]
    assert max(pd.Timestamp(r["valid_until"]) for r in imported) <= pd.Timestamp("2026-09-14T13:00Z")
