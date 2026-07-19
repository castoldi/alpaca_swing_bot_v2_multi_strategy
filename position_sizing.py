"""Shared whole-share position-sizing policy for live and backtest orders."""
from __future__ import annotations

import math
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
) -> PositionSize:
    """Return a whole-share quantity capped by equity fraction and cash."""
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
    quantity = math.floor(budget / price)
    if quantity < 1:
        return PositionSize(0, budget, 0.0, "budget_below_one_share")

    return PositionSize(quantity, budget, quantity * price)
