"""Normalization of yfinance daily bars into the shared OHLCV frame shape."""
import pandas as pd
import pytest

import yfinance_history as yh


def _raw_frame(multiindex: bool, ticker: str = "NVDA") -> pd.DataFrame:
    idx = pd.to_datetime(["2016-01-04", "2016-01-05"])
    data = {
        "Open": [0.78, 0.80], "High": [0.79, 0.81], "Low": [0.77, 0.79],
        "Close": [0.7886, 0.8013], "Volume": [1000.0, 1100.0],
    }
    frame = pd.DataFrame(data, index=idx)
    if multiindex:
        frame.columns = pd.MultiIndex.from_product([frame.columns, [ticker]])
    return frame


@pytest.mark.parametrize("multiindex", [False, True])
def test_normalize_handles_both_column_shapes(multiindex):
    """Recent yfinance versions return (field, ticker) columns even for one symbol."""
    out = yh._normalize(_raw_frame(multiindex), "NVDA")
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert len(out) == 2
    assert out["close"].iloc[0] == pytest.approx(0.7886)


def test_normalize_empty_input_is_an_empty_frame_not_an_error():
    out = yh._normalize(pd.DataFrame(), "NVDA")
    assert out.empty
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]


def test_normalize_drops_tz_and_sorts():
    idx = pd.to_datetime(["2016-01-05", "2016-01-04"]).tz_localize("America/New_York")
    frame = pd.DataFrame({
        "Open": [1, 2], "High": [1, 2], "Low": [1, 2], "Close": [1, 2], "Volume": [1, 2],
    }, index=idx)
    out = yh._normalize(frame, "NVDA")
    assert out.index.tz is None
    assert list(out.index) == sorted(out.index)


def test_normalize_dedupes_same_day_keeping_the_last():
    idx = pd.to_datetime(["2016-01-04", "2016-01-04"])
    frame = pd.DataFrame({
        "Open": [1, 2], "High": [1, 2], "Low": [1, 2], "Close": [1, 2], "Volume": [1, 2],
    }, index=idx)
    out = yh._normalize(frame, "NVDA")
    assert len(out) == 1
    assert out["close"].iloc[0] == 2


def test_normalize_missing_columns_raises():
    frame = pd.DataFrame({"Open": [1], "Close": [1]}, index=pd.to_datetime(["2016-01-04"]))
    with pytest.raises(ValueError, match="missing columns"):
        yh._normalize(frame, "NVDA")


def test_fetch_bars_rejects_non_daily_timeframe():
    """A silent daily-for-4h substitution would corrupt a strategy's indicators."""
    with pytest.raises(ValueError, match="daily"):
        yh.fetch_bars("NVDA", "2016-01-01", "2016-02-01", timeframe="4h")


def test_fetch_bars_returns_empty_frame_on_download_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(yh.yf, "download", boom)
    out = yh.fetch_bars("NVDA", "2016-01-01", "2016-02-01")
    assert out.empty


def test_fetch_bars_raises_on_failure_when_strict(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(yh.yf, "download", boom)
    with pytest.raises(RuntimeError):
        yh.fetch_bars("NVDA", "2016-01-01", "2016-02-01", strict=True)


def test_fetch_bars_passes_dates_and_auto_adjust(monkeypatch):
    captured = {}
    def fake_download(ticker, start, end, interval, auto_adjust, progress):
        captured.update(ticker=ticker, start=start, end=end,
                        interval=interval, auto_adjust=auto_adjust)
        return _raw_frame(False)
    monkeypatch.setattr(yh.yf, "download", fake_download)
    from datetime import date
    yh.fetch_bars("NVDA", date(2010, 1, 1), date(2010, 2, 1))
    assert captured["auto_adjust"] is True
    assert captured["interval"] == "1d"
    assert captured["start"] == date(2010, 1, 1)
