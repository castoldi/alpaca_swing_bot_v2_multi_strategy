from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import data_feed


def test_completed_bars_removes_current_daily_session():
    df = pd.DataFrame(
        {"close": [100.0, 101.0]},
        index=pd.to_datetime(["2026-07-17", "2026-07-18"]),
    )
    as_of = datetime(2026, 7, 18, 12, tzinfo=ZoneInfo("America/New_York"))
    out = data_feed.completed_bars(df, "1d", as_of)
    assert list(out.index) == [pd.Timestamp("2026-07-17")]


def test_completed_bars_drops_the_forming_four_hour_bucket():
    # Index is tz-naive UTC bar-start; the 16:00 bucket spans 16:00-20:00.
    df = pd.DataFrame(
        {"close": [100.0, 101.0]},
        index=pd.to_datetime(["2026-07-18 12:00", "2026-07-18 16:00"]),
    )
    as_of = datetime(2026, 7, 18, 18, 0, tzinfo=ZoneInfo("UTC"))
    out = data_feed.completed_bars(df, "4h", as_of)
    assert list(out.index) == [pd.Timestamp("2026-07-18 12:00")]


def test_completed_bars_keeps_a_finished_four_hour_bucket():
    df = pd.DataFrame(
        {"close": [100.0]}, index=pd.to_datetime(["2026-07-18 16:00"])
    )
    as_of = datetime(2026, 7, 18, 20, 0, tzinfo=ZoneInfo("UTC"))
    assert data_feed.completed_bars(df, "4h", as_of).equals(df)


def test_alpaca_timeframe_accepts_only_supported_values():
    assert data_feed._timeframe_parts("4h") == (4, "Hour")
    assert data_feed._timeframe_parts("1d") == (1, "Day")


def test_feed_options_select_requested_alpaca_feed():
    from alpaca.data.enums import DataFeed

    assert data_feed._feed_options("iex")["feed"] == DataFeed.IEX
    assert data_feed._feed_options("sip")["feed"] == DataFeed.SIP


def test_fetch_bars_strict_mode_propagates_client_failure(monkeypatch):
    class BrokenClient:
        def get_stock_bars(self, _request):
            raise RuntimeError("download failed")

    monkeypatch.setattr(data_feed, "_get_client", lambda: BrokenClient())

    with pytest.raises(RuntimeError, match="download failed"):
        data_feed.fetch_bars(
            "AMD",
            date(2024, 1, 1),
            date(2024, 1, 2),
            "4h",
            feed="sip",
            strict=True,
        )
