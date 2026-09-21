"""F15: research uses the annual execution/risk path and frozen trial inputs."""
from dataclasses import replace
from datetime import date

import pandas as pd
import pytest

import backtest_2025 as annual
import backtest_history as history
import backtest_portfolio as portfolio
from config import PARAMS, StrategyType
from research import optimizer
from strategies import REGISTRY
from tests.test_backtest_execution import bars, Signal
from tests.test_daily_loss_accounting import held, attempt, minutes
from tests.test_rsi_f14 import entry_frame


def test_historical_years_apply_daily_loss_guard_and_custom_limit():
    candidates = [held('2026-01-06 14:31', 90), attempt('2026-01-06 15:00')]
    frames = {'HELD': minutes(['2026-01-05 20:59', '2026-01-06 14:30'], [100, 90])}
    params = replace(PARAMS, position_size_pct=.5, max_daily_loss_pct=.03, tax_year_end_guard=False)
    guarded = history.run_independent_annual_portfolios({2026: candidates}, price_frames=frames, params=params)[2026]
    permissive = history.run_independent_annual_portfolios(
        {2026: candidates}, price_frames=frames, params=replace(params, max_daily_loss_pct=.1))[2026]
    assert guarded.accepted_positions == 1 and guarded.kill_switch_blocked_entries == 1
    assert permissive.accepted_positions == 2


@pytest.mark.parametrize(('strategy', 'symbols', 'timeframe'), [
    (StrategyType.TQQQ_MOMENTUM, ['TQQQ'], '4h'),
    (StrategyType.SMA_50_CROSS, ['AMD'], '1d'),
    (StrategyType.BREAKOUT, ['AMD'], '4h'),
])
def test_optimizer_loads_strategy_universe_timeframe_and_minutes(monkeypatch, strategy, symbols, timeframe):
    monkeypatch.setattr(optimizer, 'TICKERS', ['AMD'])
    loaded, executions = [], []
    def download(ticker, start, end, interval):
        loaded.append((ticker, interval))
        frame = bars(pd.date_range('2025-10-01', periods=90, freq='D'))
        frame.attrs.update(timeframe=interval, feed='iex', adjustment='all')
        return frame
    def execution(frame, ticker, start, end):
        executions.append(ticker)
        frame = bars(['2025-12-31 20:59', '2026-01-02 14:30'])
        frame.attrs.update(timeframe='1min', feed='iex', adjustment='all')
        return frame
    monkeypatch.setattr(optimizer, 'download_history', download)
    monkeypatch.setattr('backtest_execution.load_execution_bars', execution)
    dataset = optimizer.load_dataset(strategy, 2026)
    assert loaded == [(ticker, timeframe) for ticker in symbols]
    assert executions == symbols
    signals, execution_data, _ = dataset.inputs()
    assert set(signals) == {(ticker, timeframe) for ticker in symbols}
    assert set(execution_data) == set(symbols)


def test_optimizer_baseline_matches_real_annual_trades(monkeypatch):
    from research.optimizer_data import BacktestDataset
    strat = Signal()
    monkeypatch.setitem(REGISTRY, 'breakout', strat)
    monkeypatch.setattr(annual, 'TICKERS', ['TEST'])
    signals = bars(['2026-01-02 12:00', '2026-01-02 16:00'])
    signals.attrs.update(timeframe='4h', feed='iex', adjustment='all')
    execution = bars(['2025-12-31 20:59', '2026-01-02 16:00', '2026-01-02 16:01'],
                     [(100, 101, 99, 100), (100, 101, 99, 100), (100, 112, 99, 110)])
    execution.attrs.update(timeframe='1min', feed='iex', adjustment='all')
    data = {('TEST', '4h'): signals}
    dataset = BacktestDataset('breakout', 2026, data, {'TEST': execution})
    params = replace(PARAMS, tax_year_end_guard=False, position_size_pct=.3)
    reference, _, _ = annual.run_strategy_year(data, strat, 2026, params,
                                               execution_data={'TEST': execution})
    actual = optimizer.run_backtest_for_params(params, StrategyType.BREAKOUT, 2026, dataset=dataset)
    assert len(actual) == 1
    assert actual == list(reference.trades)
    assert actual[0].shares == 3 and actual[0].pnl_dollars == 30


def test_frozen_dataset_rejects_mutation_and_wrong_experiment():
    from research.optimizer_data import BacktestDataset
    source = bars(['2026-01-02'])
    source.attrs.update(timeframe='4h', feed='iex', adjustment='all')
    execution = bars(['2026-01-02 14:30'])
    execution.attrs.update(timeframe='1min', feed='iex', adjustment='all')
    dataset = BacktestDataset('breakout', 2026, {('AMD', '4h'): source}, {'AMD': execution})
    fingerprint = dataset.metadata['fingerprint']
    source.iloc[0, 0] = 1
    copied, copied_minutes, _ = dataset.inputs()
    copied[('AMD', '4h')].iloc[0, 0] = 2
    copied_minutes['AMD'].iloc[0, 0] = 3
    clean, clean_minutes, _ = dataset.inputs()
    assert clean[('AMD', '4h')].iloc[0, 0] == 100
    assert clean_minutes['AMD'].iloc[0, 0] == 100
    assert dataset.metadata['fingerprint'] == fingerprint
    with pytest.raises(ValueError, match='dataset'):
        optimizer.run_backtest_for_params(PARAMS, StrategyType.BREAKOUT, 2025, dataset=dataset)


def test_trials_preserve_baseline_freeze_inputs_and_record_duplicates(monkeypatch):
    class Dataset:
        metadata = {'fingerprint': 'frozen-test-inputs'}
    loads, trials, evaluation = [], [], []
    monkeypatch.setattr(optimizer, 'load_dataset', lambda *args: loads.append(args) or Dataset())
    def backtest(params, strategy, year, *, dataset):
        trials.append((params, dataset))
        return []
    monkeypatch.setattr(optimizer, 'run_backtest_for_params', backtest)
    monkeypatch.setitem(optimizer.PARAMETER_SPACES, 'breakout', {'breakout_stop_loss_pct': (.08, .08)})
    original = optimizer.evaluate_search
    def evaluate(configs, **kwargs):
        evaluation.append(configs)
        return original(configs, **kwargs)
    monkeypatch.setattr(optimizer, 'evaluate_search', evaluate)
    baseline = replace(PARAMS, position_size_pct=.12, max_daily_loss_pct=.07,
                       tax_year_end_guard=False, macd_signal=17)
    results, report = optimizer.random_search(StrategyType.BREAKOUT, 2026, iterations=3,
                                               baseline=baseline, n_boot=10)
    assert len(loads) == len(trials) == 1  # duplicate computations reused, attempts still recorded
    assert trials[0][0] == replace(baseline, strategy=StrategyType.BREAKOUT, breakout_stop_loss_pct=.08)
    assert len(results) == len(evaluation[0]) == report.trials == 3
    assert results[0]['search']['attempted_trials'] == 3
    assert results[0]['search']['distinct_configurations'] == 1
    assert results[0]['search']['duplicate_trials'] == 2
    assert results[0]['dataset']['fingerprint'] == 'frozen-test-inputs'
    assert results[0]['params']['macd_signal'] == 17


@pytest.mark.parametrize(('name', 'field'), [
    (name, field) for name, fields in optimizer.PARAMETER_SPACES.items() for field in fields
])
def test_every_searched_parameter_changes_its_intended_rule(name, field):
    frame = entry_frame(name)
    frame['atr'] = 2.0  # target multipliers cross floors without saturating both bounds
    if name == 'sma_50_cross':
        frame['sma_cross'] = 99.
    elif name == 'tqqq_momentum':
        # TSI warmup is 88 bars; reuse the qualifying boundary at a mature index.
        frame = pd.concat([frame.iloc[:40], frame], ignore_index=True)
        frame.index = pd.date_range('2026-01-01', periods=len(frame))
        frame['tsi'] = -1.
        frame['tsi_signal'] = 0.
        frame['ema_trend'] = 95.
        frame.loc[frame.index[-1], 'tsi'] = 1.
    elif field == 'rsi_pullback_max':
        frame['rsi'] = 55.
        frame.loc[frame.index[-1], 'rsi'] = 60.
    elif field == 'rsi_lookback':
        frame['rsi'] = 60.
        frame.loc[frame.index[-11], 'rsi'] = 40.
        frame.loc[frame.index[-1], 'rsi'] = 61.
    lo, hi = optimizer.PARAMETER_SPACES[name][field]
    low = REGISTRY[name].check_entry(frame, len(frame)-1, replace(PARAMS, **{field: lo}))
    high = REGISTRY[name].check_entry(frame, len(frame)-1, replace(PARAMS, **{field: hi}))
    if field in {'rsi_pullback_max', 'rsi_lookback'}:
        assert low is None and high is not None
    elif 'stop_loss' in field:
        assert low is not None and high is not None
        assert low.stop_loss > high.stop_loss
    else:
        assert low is not None and high is not None
        assert low.take_profit < high.take_profit


def test_custom_tax_policy_and_rates_are_not_replaced_by_global_defaults():
    from tests.test_kill_switch import _single_candidate
    candidate = _single_candidate('AMD', '2026-12-15 15:00', 100, '2026-12-16 15:00', 110)
    frame = pd.DataFrame({'close': [100.]}, index=pd.to_datetime(['2026-12-14 21:00']))
    frame.attrs['price_timestamps'] = 'observed'
    params = replace(PARAMS, tax_hard_block=True, tax_hard_block_days=40,
                     tax_short_term_rate=.5, tax_use_brackets=False, tax_niit=False)
    blocked = portfolio.run_configured_portfolio([candidate], price_frames={'AMD': frame}, params=params)
    allowed = portfolio.run_configured_portfolio([candidate], price_frames={'AMD': frame},
                                                 params=replace(params, tax_hard_block=False))
    assert blocked.tax_blocked_entries == 1
    assert allowed.trades[0].pnl_dollars == 20
    assert allowed.tax_estimate == 10


def test_custom_leveraged_cap_is_used():
    from tests.test_kill_switch import _single_candidate
    candidate = _single_candidate('TQQQ', '2026-01-05 15:00', 100, '2026-01-06 15:00', 110)
    frame = pd.DataFrame({'close': [100.]}, index=pd.to_datetime(['2026-01-02 21:00']))
    frame.attrs['price_timestamps'] = 'observed'
    params = replace(PARAMS, max_leveraged_exposure_pct=.1, tax_year_end_guard=False)
    result = portfolio.run_configured_portfolio([candidate], price_frames={'TQQQ': frame}, params=params)
    assert result.trades[0].shares == 1


def test_dataset_freezes_earnings_history_and_provenance():
    from research.optimizer_data import BacktestDataset
    signal = bars(['2026-01-02'])
    signal.attrs.update(timeframe='4h', feed='iex', adjustment='all')
    execution = bars(['2026-01-02 14:30'])
    execution.attrs.update(timeframe='1min', feed='iex', adjustment='all')
    earnings = {'AMD': [{'observed_at': '2026-01-01', 'status': 'ok', 'events': []}]}
    dataset = BacktestDataset('ensemble', 2026, {('AMD', '4h'): signal}, {'AMD': execution}, earnings)
    earnings['AMD'][0]['status'] = 'error'
    signals, _, store = dataset.inputs()
    signals[('AMD', '4h')].attrs['feed'] = 'sip'
    records = store.history('AMD', pd.Timestamp('2026-01-02'))
    assert records[0]['status'] == 'ok'
    records[0]['status'] = 'error'
    assert store.history('AMD', pd.Timestamp('2026-01-02'))[0]['status'] == 'ok'
    assert dataset.inputs()[0][('AMD', '4h')].attrs['feed'] == 'iex'
    other = BacktestDataset('ensemble', 2026, {('AMD', '4h'): signal}, {'AMD': execution}, earnings)
    assert dataset.metadata['fingerprint'] != other.metadata['fingerprint']


def test_stats_use_custom_starting_equity_and_include_first_loss():
    from tests.test_backtest_history import make_trade
    trade = make_trade(entry='2026-01-02', exit='2026-01-03', pnl=-100)
    assert optimizer.compute_stats([trade], initial_equity=2000)['max_drawdown'] == 5


def test_shared_runner_refuses_candidates_without_valuation_data():
    with pytest.raises(ValueError, match='price_frames'):
        portfolio.run_configured_portfolio([held()], price_frames={})


def test_empty_shared_runner_returns_empty_portfolio():
    result = portfolio.run_configured_portfolio([], price_frames={})
    assert result.trades == () and result.ending_equity == PARAMS.initial_backtest_equity


@pytest.mark.parametrize('iterations', [0, -1, 1.5, True])
def test_invalid_trial_count_fails_before_data_loading(monkeypatch, iterations):
    def unexpected(*args):
        pytest.fail('invalid search must not download data')
    monkeypatch.setattr(optimizer, 'load_dataset', unexpected)
    with pytest.raises(ValueError, match='iterations'):
        optimizer.random_search(StrategyType.BREAKOUT, 2026, iterations=iterations)


def test_two_distinct_trials_use_same_snapshot_without_mutating_global_random_state(monkeypatch):
    import random
    from research.optimizer_data import BacktestDataset
    source = bars(['2026-01-02'])
    source.attrs.update(timeframe='4h', feed='iex', adjustment='all')
    minute = bars(['2026-01-02 14:30'])
    minute.attrs.update(timeframe='1min', feed='iex', adjustment='all')
    dataset = BacktestDataset('breakout', 2026, {('AMD', '4h'): source}, {'AMD': minute})
    loaded, values = [], []
    monkeypatch.setattr(optimizer, 'load_dataset', lambda *args: loaded.append(1) or dataset)
    def trial(*args, dataset):
        signals, execution, _ = dataset.inputs()
        values.append((signals[('AMD', '4h')].iloc[0, 0], execution['AMD'].iloc[0, 0]))
        signals[('AMD', '4h')].iloc[0, 0] = 1
        source.iloc[0, 0] = 2  # backing cache/source revisions cannot change subsequent trials
        return []
    monkeypatch.setattr(optimizer, 'run_backtest_for_params', trial)
    state = random.getstate()
    results, _ = optimizer.random_search(StrategyType.BREAKOUT, 2026, iterations=2, n_boot=10)
    assert len(loaded) == 1 and values == [(100, 100), (100, 100)]
    assert results[0]['search']['distinct_configurations'] == 2
    assert random.getstate() == state


def test_real_sma_baseline_matches_annual_runner(monkeypatch):
    from research.optimizer_data import BacktestDataset
    monkeypatch.setattr(annual, 'TICKERS', ['AMD'])
    signal = bars(pd.date_range(end='2026-01-02 05:00', periods=51, freq='D'),
                  [(100, 101, 99, 100)] * 50 + [(101, 102, 100, 101)])
    signal.attrs.update(timeframe='1d', feed='iex', adjustment='all')
    minute = bars(['2025-12-31 20:59', '2026-01-05 14:30', '2026-01-05 14:31'],
                  [(100, 101, 99, 100), (101, 102, 100, 101), (101, 102, 100, 102)])
    minute.attrs.update(timeframe='1min', feed='iex', adjustment='all')
    data = {('AMD', '1d'): signal}
    dataset = BacktestDataset('sma_50_cross', 2026, data, {'AMD': minute})
    params = replace(PARAMS, tax_year_end_guard=False)
    expected, _, _ = annual.run_strategy_year(data, REGISTRY['sma_50_cross'], 2026, params,
                                              execution_data={'AMD': minute})
    actual = optimizer.run_backtest_for_params(params, StrategyType.SMA_50_CROSS, 2026, dataset=dataset)
    assert len(actual) == 1 and actual == list(expected.trades)


def test_dataset_rejects_mismatched_feed():
    from research.optimizer_data import BacktestDataset
    signal = bars(['2026-01-02'])
    signal.attrs.update(timeframe='4h', feed='iex', adjustment='all')
    minute = bars(['2026-01-02 14:30'])
    minute.attrs.update(timeframe='1min', feed='sip', adjustment='all')
    with pytest.raises(ValueError, match='feed'):
        BacktestDataset('breakout', 2026, {('AMD', '4h'): signal}, {'AMD': minute})
