from datetime import date, datetime, timezone

import pandas as pd
import pytest

import ibkr_history as ih
from market_cache import MarketDataCache
from tests.test_market_cache import empty_bars, recording_fetcher


def hourly(index, prices, volume=100.0):
    return pd.DataFrame(
        {"open": prices, "high": [p + 1 for p in prices], "low": [p - 1 for p in prices],
         "close": prices, "volume": [volume] * len(prices)},
        index=pd.DatetimeIndex(index),
    )


def test_resample_4h_uses_alpaca_utc_buckets():
    bars = hourly(["2022-06-06 08:00", "2022-06-06 11:00", "2022-06-06 12:00",
                   "2022-06-06 13:00", "2022-06-06 15:00"], [10, 11, 12, 13, 14])
    out = ih.resample_4h(bars)
    assert list(out.index.strftime("%H:%M")) == ["08:00", "12:00"]
    assert out.loc["2022-06-06 08:00", "open"] == 10
    assert out.loc["2022-06-06 08:00", "close"] == 11
    assert out.loc["2022-06-06 12:00", "high"] == 15
    assert out.loc["2022-06-06 12:00", "volume"] == 300


def test_resample_4h_drops_buckets_without_trades():
    bars = hourly(["2022-06-06 08:00", "2022-06-06 13:00"], [10, 12])
    bars.iloc[0, bars.columns.get_loc("volume")] = 0
    out = ih.resample_4h(bars)
    assert list(out.index.strftime("%H:%M")) == ["12:00"]


def test_daily_stamp_is_new_york_midnight_in_utc():
    idx = ih.daily_index_to_utc(["2022-01-03", "2022-07-01"])
    assert list(idx) == [pd.Timestamp("2022-01-03 05:00"), pd.Timestamp("2022-07-01 04:00")]


def test_dividend_factor_scales_prices_by_new_york_date_not_utc_date():
    days = pd.DatetimeIndex(["2025-03-31", "2025-04-01"])
    factors = ih.dividend_factors(
        pd.DataFrame({"close": [99.0, 50.0]}, index=days),
        pd.DataFrame({"close": [100.0, 50.0]}, index=days),
    )
    # 2025-04-01 00:00 UTC is still 2025-03-31 in New York -> factor 0.99.
    bars = hourly(["2025-03-31 20:00", "2025-04-01 00:00", "2025-04-01 14:00"], [100, 100, 100])
    out = ih.apply_factors(bars, factors)
    assert out["close"].round(6).tolist() == [99.0, 99.0, 100.0]
    assert out["volume"].tolist() == [100.0] * 3


def test_ibkr_partition_is_accepted_and_served_without_daily_refetch(tmp_path):
    calls, fetcher = recording_fetcher()
    day1 = MarketDataCache(tmp_path / "bars.db", fetcher=fetcher,
                           now_fn=lambda: datetime(2020, 2, 1, tzinfo=timezone.utc))
    day1.get_bars("NVDA", date(2020, 1, 1), date(2020, 1, 10), "1d", feed="ibkr", refresh=True)
    day2 = MarketDataCache(tmp_path / "bars.db", fetcher=fetcher,
                           now_fn=lambda: datetime(2020, 3, 1, tzinfo=timezone.utc))
    # New UTC day and a wider range: an Alpaca partition would re-fetch, ibkr must not.
    bars = day2.get_bars("NVDA", date(2019, 12, 1), date(2020, 2, 1), "1d", feed="ibkr")
    assert len(calls) == 1
    assert bars.index.min() == pd.Timestamp("2020-01-01")


def test_default_fetcher_never_downloads_ibkr_implicitly(tmp_path, monkeypatch):
    import market_cache as mc

    monkeypatch.setattr(mc.data_feed, "fetch_bars", lambda *a, **k: empty_bars())
    cache = MarketDataCache(tmp_path / "bars.db",
                            now_fn=lambda: datetime(2020, 2, 1, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="import_ibkr_history"):
        cache.get_bars("NVDA", date(2020, 1, 1), date(2020, 1, 10), "4h", feed="ibkr")
