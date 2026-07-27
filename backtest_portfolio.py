"""Annual whole-share portfolio simulation for strategy backtests."""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from itertools import groupby

import pandas as pd

from config import LEVERAGED_TICKERS, PARAMS, StrategyParams
from position_sizing import leveraged_headroom, whole_share_position_size
import tax as tax_mod
from strategies.base import (
    BaseStrategy,
    ExitLeg,
    SKIP_EARNINGS_STRATEGIES,
    Trade,
    add_earnings_filter,
    add_indicators,
    is_tp_reachable_in_days,
    simulate_exit,
    simulate_exit_scaleout,
)


@dataclass(frozen=True)
class BacktestCandidate:
    ticker: str
    entry_date: pd.Timestamp
    entry_price: float
    stop_loss: float
    take_profit: float
    strategy: str
    single_legs: tuple[ExitLeg, ...]
    scaled_legs: tuple[ExitLeg, ...]


@dataclass(frozen=True)
class PortfolioResult:
    trades: tuple[Trade, ...]
    starting_equity: float
    ending_equity: float
    return_pct: float
    accepted_positions: int
    skipped_positions: int
    equity_curve: tuple[tuple[pd.Timestamp, float], ...]
    # Tax view. `tax_estimate` is the liability the year's realized P&L would
    # attract; `after_tax_pnl` is gross realized P&L less that. Both are zero
    # when tax reporting is switched off, so the gross figures are unchanged.
    tax_estimate: float = 0.0
    after_tax_pnl: float = 0.0
    wash_sale_count: int = 0
    disallowed_loss: float = 0.0
    tax_blocked_entries: int = 0
    # Daily-loss kill switch view. Zero when disabled or no price_frames given.
    kill_switch_blocked_entries: int = 0
    kill_switch_trip_days: int = 0


def _trade_from_leg(
    candidate: BacktestCandidate,
    leg: ExitLeg,
    shares: int,
) -> Trade:
    pnl_pct = (leg.exit_price - candidate.entry_price) / candidate.entry_price
    return Trade(
        ticker=candidate.ticker,
        entry_date=candidate.entry_date,
        entry_price=candidate.entry_price,
        stop_loss=candidate.stop_loss,
        take_profit=candidate.take_profit,
        exit_date=pd.Timestamp(leg.exit_date),
        exit_price=leg.exit_price,
        exit_reason=leg.reason,
        bars_held=leg.bars_held,
        shares=shares,
        pnl_dollars=(leg.exit_price - candidate.entry_price) * shares,
        pnl_pct=pnl_pct,
        strategy=candidate.strategy,
    )


def materialize_candidate(
    candidate: BacktestCandidate,
    quantity: int,
) -> list[Trade]:
    """Turn a quantity-independent candidate into live-compatible exit trades.

    Live places one protected bracket (TP3 + SL) per entry regardless of
    quantity: Alpaca rejects extra concurrent sell legs (403 40310000), and a
    2024-2026 comparison showed the single bracket also outperforms the 3-leg
    scale-out. The backtest therefore models the single-exit path only;
    ``scaled_legs`` is retained on candidates for research tooling.
    """
    if quantity < 1:
        return []
    return [
        _trade_from_leg(candidate, leg, quantity)
        for leg in candidate.single_legs
    ]


def _price_asof(
    frame: pd.DataFrame | None, as_of: pd.Timestamp, *, strict: bool = False
) -> float | None:
    """Last known close before (or at-or-before) ``as_of``, else None.

    A boolean mask rather than ``Series.asof`` — ``asof`` casts ``as_of`` to
    the index's own stored datetime64 resolution and raises if that cast would
    lose precision (e.g. subtracting a nanosecond against a microsecond- or
    second-resolution index, which is exactly how a cached frame is often
    stored). A boolean comparison has no such restriction and never looks
    forward, so this stays causal either way.
    """
    if frame is None or frame.empty:
        return None
    close = frame["close"]
    mask = (close.index < as_of) if strict else (close.index <= as_of)
    matched = close.loc[mask]
    if matched.empty:
        return None
    value = matched.iloc[-1]
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return float(value)


def _mark_to_market_equity(
    cash: float,
    position_tickers: dict[int, str],
    open_remaining: dict[int, int],
    price_frames: dict[str, pd.DataFrame],
    as_of: pd.Timestamp,
    *,
    strict: bool = False,
) -> float:
    """Net liquidation value: cash plus every open position's current value.

    ``cash`` already had each position's cost basis deducted at entry, so this
    needs no separate accounting for what was paid — only what it is worth now.
    ``strict=True`` looks strictly before ``as_of`` (for "yesterday's close");
    otherwise at-or-before (for "right now", where the current bar counts).
    """
    equity = cash
    for position_id, qty in open_remaining.items():
        if qty <= 0:
            continue
        ticker = position_tickers.get(position_id)
        price = _price_asof(price_frames.get(ticker), as_of, strict=strict)
        if price is not None:
            equity += qty * price
    return equity


def run_annual_portfolio(
    candidates: list[BacktestCandidate],
    *,
    initial_equity: float,
    position_fraction: float,
    max_positions: int,
    leveraged_tickers: frozenset[str] | None = None,
    max_leveraged_fraction: float | None = None,
    apply_tax: bool | None = None,
    price_frames: dict[str, pd.DataFrame] | None = None,
    apply_kill_switch: bool | None = None,
    max_daily_loss_pct: float | None = None,
) -> PortfolioResult:
    """Run one unlevered annual portfolio with realized-P&L compounding.

    Total notional across ``leveraged_tickers`` is capped at
    ``max_leveraged_fraction`` of equity — the same rule the live bot applies,
    so backtests cannot show exposure the bot would refuse to take. Both
    default to the configured values.

    ``price_frames`` (ticker -> OHLCV, this call's own timeframe) enables the
    daily-loss kill switch: mirrors ``bot.py``'s live check, which compares
    Alpaca's mark-to-market ``account.equity`` right now against yesterday's
    closing equity, and blocks new entries once the drop reaches
    ``max_daily_loss_pct``. It re-evaluates fresh at every entry attempt rather
    than latching for the rest of the day — that's what the live code actually
    does, even though its comment says "for the rest of the day"; a later
    recovery above the threshold on the same day un-blocks new entries again,
    exactly like today's live behaviour. Exits are untouched either way — they
    were already resolved at candidate-collection time and never gate on this.
    ``apply_kill_switch`` defaults to on whenever ``price_frames`` is supplied,
    off otherwise, so existing callers that never pass frames see no change.
    """
    if not math.isfinite(initial_equity) or initial_equity <= 0:
        raise ValueError("initial_equity must be finite and positive")
    if not math.isfinite(position_fraction) or not 0 < position_fraction <= 1:
        raise ValueError("position_fraction must be finite and in (0, 1]")
    if (
        isinstance(max_positions, bool)
        or not isinstance(max_positions, int)
        or max_positions < 1
    ):
        raise ValueError("max_positions must be a positive integer")

    if leveraged_tickers is None:
        leveraged_tickers = frozenset(LEVERAGED_TICKERS)
    if max_leveraged_fraction is None:
        max_leveraged_fraction = PARAMS.max_leveraged_exposure_pct
    # The live bot refuses tax-blocked entries, so the backtest must too or the
    # two diverge — the same discipline the leveraged cap already follows.
    if apply_tax is None:
        apply_tax = PARAMS.tax_year_end_guard
    tax_blocked_entries = 0

    if apply_kill_switch is None:
        apply_kill_switch = price_frames is not None
    if apply_kill_switch and not price_frames:
        raise ValueError("apply_kill_switch requires price_frames")
    if max_daily_loss_pct is None:
        max_daily_loss_pct = PARAMS.max_daily_loss_pct
    kill_switch_blocked_entries = 0
    kill_switch_trip_days: set = set()
    kill_switch_day: pd.Timestamp | None = None
    kill_switch_day_baseline = float(initial_equity)

    cash = float(initial_equity)
    realized_pnl = 0.0
    open_leveraged_notional = 0.0
    accepted_positions = 0
    skipped_positions = 0
    accepted_trades: list[Trade] = []
    equity_curve: list[tuple[pd.Timestamp, float]] = []

    exit_events: list[tuple[pd.Timestamp, int, int, Trade]] = []
    open_remaining: dict[int, int] = {}
    open_tickers: dict[str, int] = {}
    position_tickers: dict[int, str] = {}
    event_sequence = 0
    position_sequence = 0

    def realize_before(timestamp: pd.Timestamp | None) -> None:
        nonlocal cash, realized_pnl, open_leveraged_notional
        while exit_events and (
            timestamp is None or exit_events[0][0] < timestamp
        ):
            _, _, position_id, trade = heapq.heappop(exit_events)
            cash += trade.shares * trade.exit_price
            realized_pnl += trade.pnl_dollars
            if trade.ticker in leveraged_tickers:
                # Release exactly the cost basis reserved at entry.
                open_leveraged_notional = max(
                    0.0, open_leveraged_notional - trade.shares * trade.entry_price
                )
            equity_curve.append(
                (pd.Timestamp(trade.exit_date), initial_equity + realized_pnl)
            )
            open_remaining[position_id] -= int(trade.shares)
            if open_remaining[position_id] <= 0:
                del open_remaining[position_id]
                ticker = position_tickers.pop(position_id)
                if open_tickers.get(ticker) == position_id:
                    del open_tickers[ticker]

    ordered = sorted(candidates, key=lambda item: (item.entry_date, item.ticker))
    grouped = groupby(ordered, key=lambda item: pd.Timestamp(item.entry_date))
    for entry_date, timestamp_candidates in grouped:
        # Every candidate at one timestamp is sized from the same pre-event
        # realized account. An exit dated on that bar is not known at its open.
        realize_before(entry_date)

        kill_switch_tripped_now = False
        if apply_kill_switch:
            bar_day = entry_date.normalize()
            if kill_switch_day is None or bar_day != kill_switch_day:
                kill_switch_day_baseline = _mark_to_market_equity(
                    cash, position_tickers, open_remaining, price_frames,
                    bar_day, strict=True,
                )
                kill_switch_day = bar_day
            current_equity = _mark_to_market_equity(
                cash, position_tickers, open_remaining, price_frames, entry_date
            )
            if kill_switch_day_baseline > 0:
                loss_pct = (
                    (kill_switch_day_baseline - current_equity)
                    / kill_switch_day_baseline
                )
                kill_switch_tripped_now = loss_pct >= max_daily_loss_pct
                if kill_switch_tripped_now:
                    kill_switch_trip_days.add(bar_day)

        for candidate in timestamp_candidates:

            if (
                len(open_remaining) >= max_positions
                or candidate.ticker in open_tickers
            ):
                skipped_positions += 1
                continue

            if kill_switch_tripped_now:
                kill_switch_blocked_entries += 1
                skipped_positions += 1
                continue

            # Year-end wash-sale guard, evaluated against trades already
            # realized at this point in the simulation — never the full history,
            # which would be lookahead.
            if apply_tax:
                realized_so_far = [
                    {
                        "ticker": t.ticker,
                        "status": "closed",
                        "exit_date": pd.Timestamp(t.exit_date).isoformat(),
                        "pnl_dollars": t.pnl_dollars,
                    }
                    for t in accepted_trades
                    if pd.Timestamp(t.exit_date) < entry_date
                ]
                if tax_mod.year_end_entry_block(
                    candidate.ticker,
                    entry_date.to_pydatetime(),
                    realized_so_far,
                    guard_start_month=PARAMS.tax_guard_start_month,
                    guard_start_day=PARAMS.tax_guard_start_day,
                    enabled=PARAMS.tax_year_end_guard,
                    hard_block=PARAMS.tax_hard_block,
                    hard_block_days=PARAMS.tax_hard_block_days,
                    mtm_475f=PARAMS.tax_mtm_475f,
                    crypto_symbols=PARAMS.tax_crypto_symbols,
                ):
                    tax_blocked_entries += 1
                    skipped_positions += 1
                    continue

            equity = initial_equity + realized_pnl
            is_leveraged = candidate.ticker in leveraged_tickers
            size = whole_share_position_size(
                equity,
                cash,
                candidate.entry_price,
                position_fraction,
                max_notional=(
                    leveraged_headroom(
                        equity, open_leveraged_notional, max_leveraged_fraction
                    )
                    if is_leveraged
                    else None
                ),
            )
            if size.quantity < 1:
                skipped_positions += 1
                continue

            trades = materialize_candidate(candidate, size.quantity)
            if not trades:
                skipped_positions += 1
                continue

            position_sequence += 1
            position_id = position_sequence
            accepted_positions += 1
            cash -= size.notional
            if is_leveraged:
                open_leveraged_notional += size.notional
            open_remaining[position_id] = size.quantity
            open_tickers[candidate.ticker] = position_id
            position_tickers[position_id] = candidate.ticker

            for trade in trades:
                event_sequence += 1
                accepted_trades.append(trade)
                heapq.heappush(
                    exit_events,
                    (
                        pd.Timestamp(trade.exit_date),
                        event_sequence,
                        position_id,
                        trade,
                    ),
                )

    realize_before(None)
    ending_equity = initial_equity + realized_pnl

    tax_estimate, wash_count, disallowed = _tax_view(accepted_trades)

    return PortfolioResult(
        trades=tuple(accepted_trades),
        starting_equity=float(initial_equity),
        ending_equity=ending_equity,
        return_pct=(ending_equity - initial_equity) / initial_equity,
        accepted_positions=accepted_positions,
        skipped_positions=skipped_positions,
        equity_curve=tuple(equity_curve),
        tax_estimate=round(tax_estimate, 2),
        after_tax_pnl=round(realized_pnl - tax_estimate, 2),
        wash_sale_count=wash_count,
        disallowed_loss=round(disallowed, 2),
        tax_blocked_entries=tax_blocked_entries,
        kill_switch_blocked_entries=kill_switch_blocked_entries,
        kill_switch_trip_days=len(kill_switch_trip_days),
    )


def _tax_view(trades: list[Trade]) -> tuple[float, int, float]:
    """Liability, wash-sale count and deferred loss for a set of trades.

    Reported alongside gross P&L rather than deducted from the equity curve:
    tax is assessed per year and paid the following April, so it never reduces
    the capital compounding inside the simulated year.
    """
    if not trades:
        return 0.0, 0, 0.0
    rows = [
        {
            "id": index,
            "ticker": t.ticker,
            "status": "closed",
            "entry_date": pd.Timestamp(t.entry_date).isoformat(),
            "exit_date": pd.Timestamp(t.exit_date).isoformat(),
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "shares": t.shares,
            "pnl_dollars": t.pnl_dollars,
        }
        for index, t in enumerate(trades, start=1)
    ]
    records = tax_mod.compute_tax_records(
        rows,
        mtm_475f=PARAMS.tax_mtm_475f,
        identical_groups=PARAMS.tax_identical_groups,
        crypto_symbols=PARAMS.tax_crypto_symbols,
    )
    if not records:
        return 0.0, 0, 0.0

    total_tax = 0.0
    years = {
        d.year for d in (tax_mod.parse_dt(r.sale_date) for r in records)
        if d is not None
    }
    for year in years:
        summary = tax_mod.summarize_year(
            records, year,
            PARAMS.tax_short_term_rate, PARAMS.tax_long_term_rate,
            use_brackets=PARAMS.tax_use_brackets,
            filing_status=PARAMS.tax_filing_status,
            other_income=PARAMS.tax_other_income,
            apply_niit=PARAMS.tax_niit,
            mtm_475f=PARAMS.tax_mtm_475f,
        )
        total_tax += summary["estimated_tax"]

    return (
        total_tax,
        sum(1 for r in records if r.is_wash_sale),
        sum(r.disallowed_loss for r in records),
    )


def _signal_exit_candidate(
    frame: pd.DataFrame,
    ticker: str,
    signal_idx: int,
    end_idx: int,
    params: StrategyParams,
    strategy: BaseStrategy,
) -> BacktestCandidate | None:
    entry_idx = signal_idx + 1
    if entry_idx > end_idx:
        return None

    entry_bar = frame.iloc[entry_idx]
    entry_price = float(entry_bar["open"])
    stop = entry_price * (1.0 - strategy.stop_loss_fraction(params))
    exit_idx = end_idx
    exit_price = float(frame.iloc[end_idx]["close"])
    exit_reason = "end_of_data"

    for idx in range(entry_idx, end_idx + 1):
        bar = frame.iloc[idx]
        bar_open = float(bar["open"])
        if bar_open <= stop:
            exit_idx = idx
            exit_price = bar_open
            exit_reason = "gap_stop"
            break
        if float(bar["low"]) <= stop:
            exit_idx = idx
            exit_price = stop
            exit_reason = "stop_loss"
            break
        reason = strategy.check_exit(frame, idx, params)
        if reason and idx + 1 <= end_idx:
            exit_idx = idx + 1
            exit_price = float(frame.iloc[exit_idx]["open"])
            exit_reason = reason
            break

    leg = ExitLeg(
        pd.Timestamp(frame.index[exit_idx]),
        exit_price,
        exit_reason,
        exit_idx - entry_idx,
        1.0,
    )
    return BacktestCandidate(
        ticker=ticker,
        entry_date=pd.Timestamp(entry_bar.name),
        entry_price=entry_price,
        stop_loss=stop,
        take_profit=0.0,
        strategy=strategy.name,
        single_legs=(leg,),
        scaled_legs=(leg,),
    )


def collect_backtest_candidates(
    frame: pd.DataFrame,
    ticker: str,
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
    params: StrategyParams = PARAMS,
    strategy: BaseStrategy | None = None,
) -> list[BacktestCandidate]:
    """Collect entry opportunities without applying portfolio constraints."""
    if strategy is None:
        raise ValueError("strategy must be a BaseStrategy instance")
    if frame.empty:
        return []

    data = add_indicators(frame, params)
    if strategy.name in SKIP_EARNINGS_STRATEGIES:
        data = add_earnings_filter(data, ticker, params)

    start = pd.Timestamp(window_start)
    end = pd.Timestamp(window_end)
    eligible_indexes = [
        idx for idx, timestamp in enumerate(data.index)
        if start <= pd.Timestamp(timestamp) <= end
    ]
    if not eligible_indexes:
        return []
    end_idx = eligible_indexes[-1]

    candidates: list[BacktestCandidate] = []
    for idx in eligible_indexes:
        signal = strategy.check_entry(data, idx, params)
        if signal is None:
            continue

        if strategy.exit_mode == "signal_with_stop":
            candidate = _signal_exit_candidate(
                data, ticker, idx, end_idx, params, strategy
            )
            if candidate is not None and candidate.entry_date <= end:
                candidates.append(candidate)
            continue

        if not is_tp_reachable_in_days(
            signal.entry_price, signal.tp1, signal.atr, days=4
        ):
            continue

        # Entry slippage guard, mirroring bot.py's live check exactly (including
        # its `has_take_profit` gate — signal_with_stop strategies never reach
        # this branch). The SL/TP geometry was computed off the signal bar's
        # close; live only submits the order if the market has not drifted more
        # than entry_max_slippage_pct away by execution time. There is no
        # tick-level data cached, so the next bar's open is the closest available
        # proxy for "the price a live snapshot would see shortly after the
        # signal bar closed" — the same convention already used for
        # signal_with_stop entries below. A signal on the last bar of the window
        # has no next bar to price a fill from and is skipped, matching
        # `_signal_exit_candidate`'s identical boundary rule.
        fill_idx = idx + 1
        if fill_idx > end_idx:
            continue
        fill_price = float(data.iloc[fill_idx]["open"])
        if signal.entry_price > 0:
            slippage = abs(fill_price - signal.entry_price) / signal.entry_price
            if slippage > params.entry_max_slippage_pct:
                continue

        clipped = data.iloc[: end_idx + 1]
        exit_date, exit_price, exit_reason, bars_held = simulate_exit(
            clipped, idx, signal, params
        )
        single_leg = ExitLeg(
            pd.Timestamp(exit_date),
            exit_price,
            exit_reason,
            bars_held,
            1.0,
        )
        scaled_legs = tuple(simulate_exit_scaleout(clipped, idx, signal, params))
        if not scaled_legs:
            continue
        candidates.append(
            BacktestCandidate(
                ticker=ticker,
                entry_date=pd.Timestamp(signal.date),
                entry_price=signal.entry_price,
                stop_loss=signal.stop_loss,
                take_profit=signal.tp3,
                strategy=strategy.name,
                single_legs=(single_leg,),
                scaled_legs=scaled_legs,
            )
        )
    return candidates
