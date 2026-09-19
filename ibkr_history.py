"""Historical 4h/daily bars from Interactive Brokers, shaped like the Alpaca cache.

Alpaca's history stops at 2016-01-04, so the 2000 and 2007-2009 bears can only
be studied at the index level. IBKR serves stock history back to ~1999 through
the IB Gateway that ``ibkr_trading_bot`` already runs, and this module turns it
into the exact shape ``market_cache`` stores for Alpaca:

- **4h**: UTC-anchored buckets (00/04/08/12/16/20) spanning extended hours,
  built from IBKR 1-hour ``TRADES`` bars requested with ``useRTH=False``.
  IBKR's native "4 hours" bars are session-anchored instead and would shift
  every signal, so they are never used.
- **1d**: IBKR ``ADJUSTED_LAST`` daily bars, stamped at New York midnight like
  Alpaca's daily bars.
- **Adjustment**: IBKR intraday ``TRADES`` bars are split-adjusted only, while
  Alpaca ``adjustment="all"`` is also dividend-adjusted. Each intraday bar is
  scaled by that day's ``ADJUSTED_LAST / TRADES`` daily close ratio. Validated
  against the cached SIP partition: 40/40 4h buckets matched on NVDA (2022) and
  META (2025) and daily closes matched to the cent.

Known difference: IBKR volume excludes odd lots, so it runs ~65% of SIP volume
in recent years (~95% in 2016). Strategies compare volume to its own average,
so the level shift mostly cancels, but the drift over time does not.

This is **import-only research data**. The live bot and dashboard never read
it; ``scripts/import_ibkr_history.py`` is the only thing that downloads it, and
``market_cache`` never re-fetches the ``ibkr`` partition on its own.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Union

import pandas as pd

from logger_setup import get_logger

log = get_logger(__name__)

OHLCV = ["open", "high", "low", "close", "volume"]
NY = "America/New_York"
# Same discovery order as ibkr_trading_bot: IB Gateway paper/live, TWS paper/live.
PORT_DISCOVERY_ORDER = (4002, 7497, 4001, 7496)
# Distinct from ibkr_trading_bot's ids (17, +1, +2, +100) — IBKR rejects a
# second connection on an id already in use (error 326).
CLIENT_ID = 241
# 30 trading days of hourly bars per request. Hourly bars are outside IBKR's hard
# pacing rule (which covers bars of 30s or less), but the Gateway is shared
# with the live MNQ bot, so requests are still spaced out.
CHUNK_DAYS = 30
REQUEST_SPACING_S = 2.0


def connect(host: str = "127.0.0.1", ports=PORT_DISCOVERY_ORDER, client_id: int = CLIENT_ID):
    """Read-only connection to whichever IB Gateway/TWS port is listening."""
    from ib_async import IB

    errors = []
    for port in ports:
        ib = IB()
        try:
            ib.connect(host, port, clientId=client_id, timeout=8, readonly=True)
            log.info("IBKR connected read-only on port %d (accounts %s)", port, ib.managedAccounts())
            return ib
        except Exception as exc:  # noqa: BLE001 — try the next port
            errors.append(f"{port}: {type(exc).__name__}")
            if ib.isConnected():
                ib.disconnect()
    raise ConnectionError("No IB Gateway/TWS reachable (" + ", ".join(errors) + ")")


def _as_ts(value: Union[date, datetime, pd.Timestamp]) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_convert("UTC").tz_localize(None) if ts.tzinfo else ts


def _frame(bars) -> pd.DataFrame:
    rows = [dict(timestamp=b.date, open=b.open, high=b.high, low=b.low,
                 close=b.close, volume=float(b.volume)) for b in bars]
    if not rows:
        return pd.DataFrame(columns=OHLCV)
    return pd.DataFrame(rows).set_index("timestamp")


def daily_index_to_utc(days) -> pd.DatetimeIndex:
    """Trading dates -> New York midnight in tz-naive UTC (Alpaca's 1d stamp)."""
    idx = pd.DatetimeIndex(pd.to_datetime([str(d) for d in days]))
    return idx.tz_localize(NY).tz_convert("UTC").tz_localize(None)


def dividend_factors(adjusted_daily: pd.DataFrame, raw_daily: pd.DataFrame) -> pd.Series:
    """Per-NY-date multiplier that turns split-adjusted prices into split+dividend adjusted."""
    ratio = (adjusted_daily["close"] / raw_daily["close"]).dropna()
    ratio.index = pd.DatetimeIndex(ratio.index).normalize()
    return ratio


def apply_factors(hourly_utc: pd.DataFrame, factors: pd.Series) -> pd.DataFrame:
    """Scale each intraday bar's prices by its New York trading date's factor.

    Bars on a date with no daily bar (a holiday pre-market print, or before the
    first daily bar) take the nearest earlier factor, else the first one.
    """
    if hourly_utc.empty:
        return hourly_utc
    ny_dates = (pd.DatetimeIndex(hourly_utc.index).tz_localize("UTC")
                .tz_convert(NY).tz_localize(None).normalize())
    f = factors.sort_index()
    mult = f.reindex(ny_dates, method="ffill").bfill().fillna(1.0).to_numpy()
    out = hourly_utc.copy()
    for col in ("open", "high", "low", "close"):
        out[col] = out[col].to_numpy() * mult
    return out


def resample_4h(hourly_utc: pd.DataFrame) -> pd.DataFrame:
    """Hourly bars -> Alpaca-style UTC 4h buckets; buckets with no trades are dropped."""
    if hourly_utc.empty:
        return pd.DataFrame(columns=OHLCV)
    agg = hourly_utc.resample("4h", closed="left", label="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna(subset=["open", "close"])
    return agg[agg["volume"] > 0]


class IBKRHistory:
    """Fetcher bound to one live IB connection; matches ``market_cache.Fetcher``."""

    def __init__(self, ib, spacing_s: float = REQUEST_SPACING_S):
        self.ib = ib
        self.spacing_s = spacing_s
        self._daily_cache: dict[tuple[str, str], pd.DataFrame] = {}

    def _contract(self, ticker: str):
        from ib_async import Stock

        contract = Stock(ticker, "SMART", "USD", primaryExchange="")
        qualified = self.ib.qualifyContracts(contract)
        if not qualified:
            raise ValueError(f"IBKR could not qualify {ticker}")
        return qualified[0]

    def _request(self, contract, end: str, duration: str, bar: str, what: str, rth: bool):
        for attempt in range(3):
            bars = self.ib.reqHistoricalData(contract, end, duration, bar, what,
                                             useRTH=rth, formatDate=2, timeout=180)
            time.sleep(self.spacing_s)
            if bars is not None:
                return bars
            log.warning("IBKR %s %s %s end=%s timed out (attempt %d)",
                        contract.symbol, bar, what, end, attempt + 1)
            time.sleep(15 * (attempt + 1))
        raise TimeoutError(f"IBKR history request failed: {contract.symbol} {bar} {what} end={end}")

    def daily(self, ticker: str, what: str) -> pd.DataFrame:
        """Full daily history (one request; ADJUSTED_LAST only allows an open end)."""
        key = (ticker, what)
        if key not in self._daily_cache:
            bars = self._request(self._contract(ticker), "", "30 Y", "1 day", what, True)
            frame = _frame(bars)
            frame.index = daily_index_to_utc(frame.index)
            self._daily_cache[key] = frame[OHLCV].astype(float)
        return self._daily_cache[key]

    def hourly(self, ticker: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        """Split-adjusted hourly TRADES in [start, end), extended hours, oldest first."""
        contract = self._contract(ticker)
        head = self.ib.reqHeadTimeStamp(contract, "TRADES", useRTH=False, formatDate=2)
        if head:
            start = max(start, _as_ts(head))
        chunks, cursor = [], end
        while cursor > start:
            stamp = cursor.strftime("%Y%m%d-%H:%M:%S")
            frame = _frame(self._request(contract, stamp, f"{CHUNK_DAYS} D", "1 hour", "TRADES", False))
            if frame.empty:
                # A hole inside the listed history: keep walking back to the head stamp.
                log.warning("IBKR %s hourly: empty chunk ending %s", ticker, stamp)
                cursor -= timedelta(days=CHUNK_DAYS)
                continue
            frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index, utc=True)).tz_localize(None)
            chunks.append(frame)
            # "30 D" is 30 *trading* days (~6 calendar weeks); resume right
            # before the earliest bar returned instead of a fixed step.
            cursor = min(cursor - timedelta(days=CHUNK_DAYS), frame.index.min())
            log.info("IBKR %s hourly: %d bars %s -> %s", ticker, len(frame),
                     frame.index.min(), frame.index.max())
        if not chunks:
            return pd.DataFrame(columns=OHLCV)
        hourly = pd.concat(chunks).sort_index()
        hourly = hourly[~hourly.index.duplicated(keep="last")]
        return hourly[(hourly.index >= start) & (hourly.index < end)][OHLCV].astype(float)

    def __call__(self, ticker: str, start, end, timeframe: str, *, feed: str = "ibkr",
                 strict: bool = False) -> pd.DataFrame:
        start_ts, end_ts = _as_ts(start), _as_ts(end)
        adjusted = self.daily(ticker, "ADJUSTED_LAST")
        if timeframe == "1d":
            return adjusted[(adjusted.index >= start_ts) & (adjusted.index < end_ts)]
        if timeframe != "4h":
            raise ValueError(f"IBKR import supports 1d and 4h, not {timeframe!r}")
        raw = self.daily(ticker, "TRADES")
        factors = dividend_factors(
            adjusted.set_axis(adjusted.index.tz_localize("UTC").tz_convert(NY).tz_localize(None)),
            raw.set_axis(raw.index.tz_localize("UTC").tz_convert(NY).tz_localize(None)),
        )
        hourly = self.hourly(ticker, start_ts, end_ts)
        return resample_4h(apply_factors(hourly, factors))
