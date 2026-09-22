"""Shared whole-share position-sizing policy for live and backtest orders."""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class PositionSize:
    quantity: int
    budget: float
    notional: float
    reason: str | None = None


def whole_share_position_size(
    equity: float,
    cash: float,
    price: float,
    fraction: float,
    max_notional: float | None = None,
) -> PositionSize:
    """Return a whole-share quantity capped by equity fraction, cash, and an
    optional hard notional ceiling.

    ``max_notional`` carries a group-level limit the per-position fraction
    cannot express — currently the leveraged-ETF exposure cap, which has to
    consider every open leveraged position at once rather than this one entry.
    """
    values = (equity, cash, price, fraction)
    if (
        not all(math.isfinite(value) for value in values)
        or equity <= 0
        or cash < 0
        or price <= 0
        or not 0 < fraction <= 1
    ):
        return PositionSize(0, 0.0, 0.0, "invalid_input")

    budget = min(equity * fraction, cash)
    if max_notional is not None:
        if not math.isfinite(max_notional):
            return PositionSize(0, 0.0, 0.0, "invalid_input")
        capped = min(budget, max(0.0, max_notional))
        if capped < budget:
            budget = capped
            if budget < price:
                return PositionSize(0, budget, 0.0, "group_exposure_cap")

    quantity = math.floor(budget / price)
    if quantity < 1:
        return PositionSize(0, budget, 0.0, "budget_below_one_share")

    return PositionSize(quantity, budget, quantity * price)


def group_headroom(
    equity: float,
    ticker: str,
    open_notional_by_ticker: Mapping[str, float],
    groups: Iterable[tuple[str, Iterable[str], float]],
) -> float | None:
    """Dollars ``ticker`` may still add under every exposure group it is in.

    None when the ticker belongs to no group (no group limit applies). An
    unreadable (non-finite) open notional in a group leaves zero headroom.
    """
    limits = []
    for _name, members, cap_fraction in groups:
        members = set(members)
        if ticker not in members:
            continue
        used = sum(max(0.0, float(open_notional_by_ticker.get(m, 0.0))) for m in members)
        limits.append(leveraged_headroom(equity, used, cap_fraction)
                      if math.isfinite(used) else 0.0)
    return min(limits) if limits else None


def combine_headroom(*limits: float | None) -> float | None:
    """The tightest of several optional notional ceilings."""
    present = [limit for limit in limits if limit is not None]
    return min(present) if present else None


def leveraged_headroom(
    equity: float,
    open_leveraged_notional: float,
    cap_fraction: float,
) -> float:
    """Dollars still available for leveraged instruments, never negative."""
    if (
        not all(
            math.isfinite(v)
            for v in (equity, open_leveraged_notional, cap_fraction)
        )
        or equity <= 0
        or cap_fraction < 0
    ):
        return 0.0
    return max(0.0, equity * cap_fraction - max(0.0, open_leveraged_notional))
