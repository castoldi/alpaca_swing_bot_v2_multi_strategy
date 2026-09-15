"""F09: a cache extension cannot splice adjusted-price generations."""
from datetime import datetime, timezone
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from market_cache import MarketDataCache
from tests.test_market_cache import sample_bars


class Source:
    def __init__(self):
        self.frame = sample_bars('2020-01-01', 10).astype(float)
        self.frame[['open', 'close']] = 100.
        self.frame['high'] = 101.
        self.frame['low'] = 99.
        self.calls = []
        self.now = datetime(2020, 2, 1, tzinfo=timezone.utc)

    def __call__(self, ticker, start, end, timeframe, **kwargs):
        self.calls.append((pd.Timestamp(start), pd.Timestamp(end)))
        return self.frame[(self.frame.index >= start) & (self.frame.index < end)].copy()

    def adjust(self, factor):
        self.frame[['open', 'high', 'low', 'close']] *= factor


def cache_for(tmp_path, source):
    return MarketDataCache(tmp_path / 'bars.db', fetcher=source, now_fn=lambda: source.now)


@pytest.mark.parametrize('factor', [.5, .98])
@pytest.mark.parametrize('initial', [('2020-01-01', '2020-01-03'),
                                    ('2020-01-03', '2020-01-05')])
def test_split_or_dividend_revision_replaces_old_prices_on_extension(tmp_path, factor, initial):
    source = Source()
    cache = cache_for(tmp_path, source)
    cache.get_bars('TEST', *initial, '1d')
    source.adjust(factor)
    result = cache.get_bars('TEST', '2020-01-01', '2020-01-06', '1d')
    assert result.close.tolist() == [100 * factor] * 5
    fresh = MarketDataCache(tmp_path / 'fresh.db', fetcher=source, now_fn=lambda: source.now)
    pd.testing.assert_frame_equal(result, fresh.get_bars('TEST', '2020-01-01', '2020-01-06', '1d'))


def test_expired_covered_range_refreshes_without_needing_new_dates(tmp_path):
    source = Source()
    cache = cache_for(tmp_path, source)
    first = cache.get_bars('TEST', '2020-01-01', '2020-01-04', '1d')
    source.adjust(.5)
    source.now = datetime(2020, 2, 2, tzinfo=timezone.utc)
    result = cache.get_bars('TEST', '2020-01-01', '2020-01-04', '1d')
    assert result.close.tolist() == [50.] * 3
    assert first.close.tolist() == [100.] * 3  # caller's old frame remains immutable


def test_explicit_refresh_replaces_and_removes_withdrawn_rows(tmp_path):
    source = Source()
    cache = cache_for(tmp_path, source)
    cache.get_bars('TEST', '2020-01-01', '2020-01-05', '1d')
    source.frame = source.frame.drop(pd.Timestamp('2020-01-02'))
    source.frame.loc[pd.Timestamp('2020-01-03'), 'close'] = 100.5
    result = cache.get_bars('TEST', '2020-01-01', '2020-01-05', '1d', refresh=True)
    assert pd.Timestamp('2020-01-02') not in result.index
    assert result.loc['2020-01-03', 'close'] == 100.5
    assert len(result) == 3


@pytest.mark.parametrize('failure', ['exception', 'invalid', 'empty'])
def test_failed_refresh_preserves_previous_complete_snapshot(tmp_path, failure):
    source = Source()
    cache = cache_for(tmp_path, source)
    original = cache.get_bars('TEST', '2020-01-01', '2020-01-04', '1d')
    original_status = cache.status()
    def broken(*args, **kwargs):
        if failure == 'exception':
            raise RuntimeError('provider down')
        if failure == 'empty':
            return source.frame.iloc[0:0]
        frame = source.frame.copy()
        frame.loc[frame.index[0], 'close'] = float('nan')
        return frame
    cache.fetcher = broken
    with pytest.raises((RuntimeError, ValueError)):
        cache.get_bars('TEST', '2020-01-01', '2020-01-05', '1d')
    assert cache.status() == original_status
    pd.testing.assert_frame_equal(cache.get_bars('TEST', '2020-01-01', '2020-01-04', '1d'), original)


def test_snapshot_fingerprint_and_read_manifest_survive_refresh(tmp_path):
    source = Source()
    cache = cache_for(tmp_path, source)
    first = cache.get_bars('TEST', '2020-01-01', '2020-01-04', '1d')
    source.adjust(.5)
    second = cache.get_bars('TEST', '2020-01-01', '2020-01-05', '1d')
    older = first.attrs['cache_snapshot']
    newer = second.attrs['cache_snapshot']
    assert older['snapshot_id'] != newer['snapshot_id']
    assert older['content_sha256'] != newer['content_sha256']
    with sqlite3.connect(cache.path) as con:
        ids = {r[0] for r in con.execute('select snapshot_id from cache_snapshots')}
        reads = con.execute('select run_id, snapshot_id, requested_start, requested_end from cache_reads').fetchall()
    assert ids == {older['snapshot_id'], newer['snapshot_id']}
    assert [r[1] for r in reads] == [older['snapshot_id'], newer['snapshot_id']]
    assert reads[0][0] == reads[1][0] and reads[0][0]
    manifest = cache.read_manifest(first.attrs['cache_read_run_id'])
    assert [r['snapshot_id'] for r in manifest] == [older['snapshot_id'], newer['snapshot_id']]
    assert [r['content_sha256'] for r in manifest] == [older['content_sha256'], newer['content_sha256']]


def test_repeated_refresh_is_a_whole_snapshot_and_preserves_fingerprint(tmp_path):
    source = Source()
    cache = cache_for(tmp_path, source)
    first = cache.get_bars('TEST', '2020-01-01', '2020-01-04', '1d')
    second = cache.get_bars('TEST', '2020-01-01', '2020-01-04', '1d', refresh=True)
    third = cache.get_bars('TEST', '2020-01-01', '2020-01-04', '1d')
    assert first.attrs['cache_snapshot']['content_sha256'] == second.attrs['cache_snapshot']['content_sha256']
    assert first.attrs['cache_snapshot']['snapshot_id'] != second.attrs['cache_snapshot']['snapshot_id']
    assert second.attrs['cache_snapshot'] == third.attrs['cache_snapshot']
    assert len(source.calls) == 2


def test_refreshing_a_subset_replaces_the_full_old_coverage(tmp_path):
    source = Source()
    cache = cache_for(tmp_path, source)
    cache.get_bars('TEST', '2020-01-01', '2020-01-08', '1d')
    source.adjust(.5)
    cache.get_bars('TEST', '2020-01-03', '2020-01-05', '1d', refresh=True)
    whole = cache.get_bars('TEST', '2020-01-01', '2020-01-08', '1d')
    assert whole.close.tolist() == [50.] * 7
    assert source.calls[-1] == (pd.Timestamp('2020-01-01'), pd.Timestamp('2020-01-08'))


def test_two_sided_failure_cannot_publish_a_successful_prefix(tmp_path):
    source = Source()
    cache = cache_for(tmp_path, source)
    original = cache.get_bars('TEST', '2020-01-03', '2020-01-05', '1d')
    def fails_on_suffix(ticker, start, end, timeframe, **kwargs):
        if end > pd.Timestamp('2020-01-05'):
            raise RuntimeError('suffix unavailable')
        return source(ticker, start, end, timeframe, **kwargs)
    cache.fetcher = fails_on_suffix
    with pytest.raises(RuntimeError):
        cache.get_bars('TEST', '2020-01-01', '2020-01-08', '1d')
    status = cache.status()[0]
    assert status['requested_start'] == '2020-01-03T00:00:00'
    assert status['snapshot_id'] == original.attrs['cache_snapshot']['snapshot_id']


def test_legacy_unversioned_rows_are_refreshed_before_use(tmp_path):
    source = Source()
    cache = cache_for(tmp_path, source)
    cache.get_bars('TEST', '2020-01-01', '2020-01-04', '1d')
    with sqlite3.connect(cache.path) as con:
        # Simulate the absence of trustworthy generation metadata in a V1 cache.
        con.execute('update coverage set snapshot_id=NULL')
    source.adjust(.5)
    result = cache.get_bars('TEST', '2020-01-01', '2020-01-04', '1d')
    assert result.close.tolist() == [50.] * 3


def test_concurrent_extensions_leave_one_whole_generation(tmp_path):
    source = Source()
    cache = cache_for(tmp_path, source)
    cache.get_bars('TEST', '2020-01-03', '2020-01-05', '1d')
    source.adjust(.5)
    def extend(end):
        other = cache_for(tmp_path, source)
        return other.get_bars('TEST', '2020-01-01', end, '1d')
    with ThreadPoolExecutor(2) as pool:
        frames = list(pool.map(extend, ['2020-01-06', '2020-01-08']))
    assert all((frame.close == 50).all() for frame in frames)
    assert cache.status()[0]['requested_end'] == '2020-01-08T00:00:00'


def test_response_rows_outside_requested_range_are_not_cached(tmp_path):
    source = Source()
    cache = cache_for(tmp_path, source)
    cache.fetcher = lambda *args, **kwargs: source.frame
    cache.get_bars('TEST', '2020-01-03', '2020-01-05', '1d')
    assert cache.status()[0]['bar_count'] == 2
