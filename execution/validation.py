"""Numeric checks at execution and account persistence boundaries."""
from __future__ import annotations

from math import isfinite


def finite_number(value: object, name: str, *, positive: bool = False, nonnegative: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number.") from exc
    if not isfinite(number):
        raise ValueError(f"{name} must be a finite number.")
    if positive and number <= 0.0:
        raise ValueError(f"{name} must be positive.")
    if nonnegative and number < 0.0:
        raise ValueError(f"{name} cannot be negative.")
    return number


def validate_account(account) -> None:
    finite_number(account.nav, "Account NAV", positive=True)
    for name in ("settled_cash", "available_cash", "buying_power", "total_commission"):
        finite_number(getattr(account, name), name)
    finite_number(account.unsettled_cash, "Unsettled cash")
    finite_number(account.dividend_receivable, "Dividend receivable", nonnegative=True)
    if account.high_water is not None:
        finite_number(account.high_water, "High water", positive=True)
    for ticker, position in account.positions.items():
        finite_number(position.quantity, f"{ticker} quantity")
        finite_number(position.market_value, f"{ticker} market value")


def validate_fill(fill) -> None:
    finite_number(fill.quantity, "Fill quantity", positive=True)
    finite_number(fill.price, "Fill price", positive=True)
    finite_number(fill.commission, "Fill commission", nonnegative=True)
    finite_number(fill.implementation_shortfall_bps, "Fill shortfall")
    finite_number(fill.quantity * fill.price + fill.commission, "Fill cash amount")
