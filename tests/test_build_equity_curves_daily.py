"""Stitching yfinance (pre-2016) with Alpaca (2016+) for the daily curve builder."""
from datetime import date

import pandas as pd
import pytest

import scripts.build_equity_curves as bec


def bars(start: str, periods: int, base: float = 100.0) -> pd.DataFrame:
    index = pd.date_range(start, periods=periods, freq="D")
    return pd.DataFrame({
        "open": base, "high": base + 1, "low": base - 1,
        "close": [base + i for i in range(periods)], "volume": 1000.0,
    }, index=index)


@pytest.fixture
def recorder(monkeypatch):
    """Stub _MARKET_CACHE.get_bars, recording every call and its feed."""
    calls = []

    def fake_get_bars(ticker, start, end, timeframe, *, feed):
        calls.append({
            "ticker": ticker, "start": pd.Timestamp(start), "end": pd.Timestamp(end),
            "timeframe": timeframe, "feed": feed,
        })
        if feed == "yfinance":
            return bars("2007-01-01", 10)
        return bars("2016-01-01", 10)

    monkeypatch.setattr(bec._MARKET_CACHE, "get_bars", fake_get_bars)
    return calls


def test_pre_floor_start_fetches_both_feeds(recorder):
    bec.download_daily_history("NVDA", date(2008, 1, 1), date(2020, 1, 1))
    feeds = {c["feed"] for c in recorder}
    assert feeds == {"yfinance", "sip"}


def test_boundary_is_half_open_with_no_overlap(recorder):
    bec.download_daily_history("NVDA", date(2008, 1, 1), date(2020, 1, 1))
    yf_call = next(c for c in recorder if c["feed"] == "yfinance")
    sip_call = next(c for c in recorder if c["feed"] == "sip")
    # yfinance's end is exactly Alpaca's start: [start, floor) then [floor, end].
    assert yf_call["end"] == pd.Timestamp(bec.ALPACA_DAILY_FLOOR)
    assert sip_call["start"] == pd.Timestamp(bec.ALPACA_DAILY_FLOOR)


def test_post_floor_start_never_calls_yfinance(recorder):
    """A strategy already starting at/after 2016 must not pay a yfinance round trip."""
    bec.download_daily_history("NVDA", date(2020, 1, 1), date(2021, 1, 1))
    assert {c["feed"] for c in recorder} == {"sip"}


def test_warmup_lookback_can_cross_the_floor_even_when_start_is_after_it(recorder):
    """A start just after 2016 still needs ~90 days of pre-start warmup, which
    dips back before the floor — that warmup must come from yfinance."""
    bec.download_daily_history("NVDA", date(2016, 2, 1), date(2016, 6, 1))
    assert {c["feed"] for c in recorder} == {"yfinance", "sip"}


def test_result_is_deduped_and_sorted(monkeypatch):
    def fake_get_bars(ticker, start, end, timeframe, *, feed):
        if feed == "yfinance":
            return bars("2015-12-28", 5)          # runs into 2016-01-01
        return bars("2016-01-01", 5)
    monkeypatch.setattr(bec._MARKET_CACHE, "get_bars", fake_get_bars)

    out = bec.download_daily_history("NVDA", date(2008, 1, 1), date(2016, 1, 10))
    assert list(out.index) == sorted(out.index)
    assert not out.index.duplicated().any()


def test_empty_segments_yield_an_empty_frame_not_a_crash(monkeypatch):
    monkeypatch.setattr(
        bec._MARKET_CACHE, "get_bars",
        lambda *a, **k: pd.DataFrame(columns=["open", "high", "low", "close", "volume"]),
    )
    out = bec.download_daily_history("NVDA", date(2008, 1, 1), date(2020, 1, 1))
    assert out.empty


def test_a_single_missing_segment_still_returns_the_other(monkeypatch):
    """yfinance has nothing for a ticker that IPO'd after 2008 (e.g. ARM in 2023)."""
    def fake_get_bars(ticker, start, end, timeframe, *, feed):
        if feed == "yfinance":
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return bars("2016-01-01", 10)
    monkeypatch.setattr(bec._MARKET_CACHE, "get_bars", fake_get_bars)

    out = bec.download_daily_history("ARM", date(2008, 1, 1), date(2020, 1, 1))
    assert not out.empty
    assert out.index.min() >= pd.Timestamp("2016-01-01")
