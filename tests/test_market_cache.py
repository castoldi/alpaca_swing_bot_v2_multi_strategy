from datetime import date, datetime, timezone

import pandas as pd
import pytest

from market_cache import MarketDataCache


def sample_bars(start: str, periods: int) -> pd.DataFrame:
    index = pd.date_range(start, periods=periods, freq="D")
    return pd.DataFrame(
        {
            "open": range(100, 100 + periods),
            "high": range(101, 101 + periods),
            "low": range(99, 99 + periods),
            "close": range(100, 100 + periods),
            "volume": [1_000] * periods,
        },
        index=index,
    )


def empty_bars() -> pd.DataFrame:
    return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def recording_fetcher():
    calls = []

    def fetcher(ticker, start, end, timeframe, **kwargs):
        start_dt = pd.Timestamp(start).to_pydatetime()
        end_dt = pd.Timestamp(end).to_pydatetime()
        calls.append((ticker, start_dt, end_dt, timeframe, kwargs))
        index = pd.date_range(start_dt, end_dt, freq="D", inclusive="left")
        if len(index) == 0:
            return empty_bars()
        return sample_bars(index[0].isoformat(), len(index))

    return calls, fetcher


def fixed_cache(tmp_path, fetcher, now=None):
    instant = now or datetime(2020, 2, 1, tzinfo=timezone.utc)
    return MarketDataCache(
        tmp_path / "bars.db", fetcher=fetcher, now_fn=lambda: instant
    )


def test_cache_populates_once_then_serves_without_fetch(tmp_path):
    calls = []
    source = sample_bars("2020-01-02", periods=3)

    def fetcher(ticker, start, end, timeframe, **kwargs):
        calls.append((ticker, start, end, timeframe, kwargs))
        return source

    cache = fixed_cache(tmp_path, fetcher)

    first = cache.get_bars(
        "AMD", date(2020, 1, 1), date(2020, 1, 10), "1d"
    )
    second = cache.get_bars(
        "AMD", date(2020, 1, 1), date(2020, 1, 10), "1d"
    )

    assert first.equals(second)
    assert len(calls) == 1
    assert calls[0][4] == {"feed": "sip", "strict": True}


def test_wider_request_fetches_only_missing_prefix_and_suffix(tmp_path):
    calls, fetcher = recording_fetcher()
    cache = fixed_cache(tmp_path, fetcher)

    cache.get_bars("AMD", date(2020, 1, 5), date(2020, 1, 10), "1d")
    cache.get_bars("AMD", date(2020, 1, 1), date(2020, 1, 15), "1d")

    assert [(call[1].date(), call[2].date()) for call in calls] == [
        (date(2020, 1, 5), date(2020, 1, 10)),
        (date(2020, 1, 1), date(2020, 1, 5)),
        (date(2020, 1, 10), date(2020, 1, 15)),
    ]


def test_overlapping_bars_are_upserted_without_duplicates(tmp_path):
    calls = []

    def fetcher(*args, **kwargs):
        calls.append((args, kwargs))
        return sample_bars("2020-01-02", 3)

    cache = fixed_cache(tmp_path, fetcher)
    cache.get_bars("AMD", date(2020, 1, 5), date(2020, 1, 10), "1d")
    out = cache.get_bars(
        "AMD", date(2020, 1, 1), date(2020, 1, 15), "1d"
    )

    assert len(calls) == 3
    assert out.index.is_unique
    assert len(out) == 3


def test_empty_successful_range_is_recorded_as_covered(tmp_path):
    calls = []

    def fetcher(*args, **kwargs):
        calls.append((args, kwargs))
        return empty_bars()

    cache = fixed_cache(tmp_path, fetcher)
    cache.get_bars("ARM", date(2016, 1, 1), date(2016, 2, 1), "1d")
    cache.get_bars("ARM", date(2016, 1, 1), date(2016, 2, 1), "1d")

    assert len(calls) == 1
    assert cache.status()[0]["bar_count"] == 0


def test_failed_fetch_does_not_advance_coverage(tmp_path):
    def broken(*args, **kwargs):
        raise RuntimeError("network down")

    cache = fixed_cache(tmp_path, broken)

    with pytest.raises(RuntimeError, match="network down"):
        cache.get_bars(
            "AMD", date(2020, 1, 1), date(2020, 1, 10), "1d"
        )

    assert cache.status() == []


def test_future_end_is_clamped_to_completed_data_ceiling(tmp_path):
    calls, fetcher = recording_fetcher()
    cache = fixed_cache(
        tmp_path,
        fetcher,
        now=datetime(2020, 1, 10, 18, tzinfo=timezone.utc),
    )

    cache.get_bars("AMD", date(2020, 1, 1), date(2021, 1, 1), "1d")

    assert calls[0][2] == datetime(2020, 1, 10)


def test_feed_is_part_of_the_cache_series_key(tmp_path):
    calls, fetcher = recording_fetcher()
    cache = fixed_cache(tmp_path, fetcher)

    cache.get_bars(
        "AMD", date(2020, 1, 1), date(2020, 1, 10), "1d", feed="iex"
    )
    cache.get_bars(
        "AMD", date(2020, 1, 1), date(2020, 1, 10), "1d", feed="sip"
    )

    assert len(calls) == 2
    assert {call[4]["feed"] for call in calls} == {"iex", "sip"}
