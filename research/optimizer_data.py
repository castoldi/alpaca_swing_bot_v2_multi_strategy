"""Experiment-local snapshots: never refetch prices or earnings between trials."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import pandas as pd

from backtest_execution import validate_execution_provenance


class FrozenEarningsHistory:
    def __init__(self, records):
        self._records = deepcopy(records)

    def history(self, ticker, as_of):
        # apply_filter selects the point-in-time snapshot for each decision.
        return deepcopy(self._records.get(ticker, []))


class BacktestDataset:
    """Private copies with fresh trial copies; identity covers prices and provenance."""
    def __init__(self, strategy, year, signals, execution, earnings=None):
        self.strategy, self.year = strategy, year
        self._signals = deepcopy(signals)
        self._execution = deepcopy(execution)
        self._earnings = deepcopy(earnings or {})
        digest = hashlib.sha256()
        digest.update(json.dumps([strategy, year, self._earnings], sort_keys=True, default=str).encode())
        sources = []
        for key, frame in sorted(self._signals.items()):
            ticker, timeframe = key
            if frame.attrs.get('timeframe') != timeframe:
                raise ValueError('dataset signal timeframe does not match its key')
            minute = self._execution[ticker]
            validate_execution_provenance(frame, minute)
        for kind, frames in [('signal', self._signals), ('execution', self._execution)]:
            for key, frame in sorted(frames.items()):
                digest.update(json.dumps([kind, key, list(frame.columns), frame.attrs],
                                         sort_keys=True, default=str).encode())
                digest.update(pd.util.hash_pandas_object(frame, index=True).values.tobytes())
                sources.append(dict(kind=kind, key=key, rows=len(frame), attrs=deepcopy(frame.attrs)))
        self._metadata = dict(fingerprint=digest.hexdigest(), strategy=strategy, year=year,
                              sources=sources)

    @property
    def metadata(self):
        return deepcopy(self._metadata)

    def inputs(self):
        return deepcopy(self._signals), deepcopy(self._execution), FrozenEarningsHistory(self._earnings)
