from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

import data_feed


def test_completed_bars_removes_current_daily_session():
    df = pd.DataFrame(
        {"close": [100.0, 101.0]},
        index=pd.to_datetime(["2026-07-17", "2026-07-18"]),
    )
    as_of = datetime(2026, 7, 18, 12, tzinfo=ZoneInfo("America/New_York"))
    out = data_feed.completed_bars(df, "1d", as_of)
    assert list(out.index) == [pd.Timestamp("2026-07-17")]


def test_completed_bars_does_not_trim_four_hour_data():
    df = pd.DataFrame(
        {"close": [100.0]}, index=pd.to_datetime(["2026-07-18 16:00"])
    )
    assert data_feed.completed_bars(df, "4h").equals(df)


def test_alpaca_timeframe_accepts_only_supported_values():
    assert data_feed._timeframe_parts("4h") == (4, "Hour")
    assert data_feed._timeframe_parts("1d") == (1, "Day")
