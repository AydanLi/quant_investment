from __future__ import annotations

from datetime import date
import math
from typing import List, Optional


def _finite_number(value: object) -> Optional[float]:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _validate_number(
    errors: List[str],
    label: str,
    value: float,
    minimum: float,
    maximum: float,
) -> None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        errors.append(f"{label} 必须是数字。")
        return
    if not math.isfinite(numeric):
        errors.append(f"{label} 不能是 NaN 或无穷值。")
    elif numeric < minimum or numeric > maximum:
        errors.append(f"{label} 必须在 {minimum:g} 到 {maximum:g} 之间。")


def validate_experiment_parameters(
    *,
    history_limit: int,
    scenario_name: str,
    start_date: str,
    rebalance_frequency: str,
    top_n: int,
    min_momentum_threshold: float,
    target_annual_vol: float,
    max_asset_weight: float,
    risk_off_cash_weight: float,
    vix_risk_off_threshold: float,
    vix_high_threshold: float,
    trading_cost_bps: float,
    today: Optional[date] = None,
) -> List[str]:
    """Return user-facing validation errors for Dashboard experiment inputs."""
    errors: List[str] = []
    current_date = today or date.today()

    if not scenario_name or not scenario_name.strip():
        errors.append("Scenario Name 不能为空。")
    elif len(scenario_name.strip()) > 200:
        errors.append("Scenario Name 不能超过 200 个字符。")

    try:
        parsed_start = date.fromisoformat(start_date.strip())
    except (AttributeError, ValueError):
        errors.append("Start Date 必须使用 YYYY-MM-DD 格式。")
    else:
        if parsed_start > current_date:
            errors.append("Start Date 不能晚于今天。")

    if rebalance_frequency not in {"D", "W", "M"}:
        errors.append("Rebalance Frequency 必须是 D、W 或 M。")

    history_numeric = _finite_number(history_limit)
    if (
        isinstance(history_limit, bool)
        or history_numeric is None
        or not history_numeric.is_integer()
    ):
        errors.append("读取最近实验数量必须是整数。")
    _validate_number(errors, "读取最近实验数量", history_limit, 5, 100)

    top_n_numeric = _finite_number(top_n)
    if (
        isinstance(top_n, bool)
        or top_n_numeric is None
        or not top_n_numeric.is_integer()
    ):
        errors.append("Top N Assets 必须是整数。")
    _validate_number(errors, "Top N Assets", top_n, 1, 6)
    _validate_number(
        errors,
        "Min Momentum Threshold",
        min_momentum_threshold,
        -0.10,
        0.20,
    )
    _validate_number(errors, "Target Annual Vol", target_annual_vol, 0.05, 0.30)
    _validate_number(errors, "Max Asset Weight", max_asset_weight, 0.10, 1.00)
    _validate_number(
        errors,
        "Risk-Off Cash Weight",
        risk_off_cash_weight,
        0.00,
        1.00,
    )
    _validate_number(
        errors,
        "VIX Risk-Off Threshold",
        vix_risk_off_threshold,
        15.0,
        50.0,
    )
    _validate_number(
        errors,
        "VIX High Threshold",
        vix_high_threshold,
        12.0,
        40.0,
    )
    _validate_number(errors, "Trading Cost (bps)", trading_cost_bps, 0.0, 30.0)

    high_vix = _finite_number(vix_high_threshold)
    risk_off_vix = _finite_number(vix_risk_off_threshold)
    if (
        high_vix is not None
        and risk_off_vix is not None
        and high_vix >= risk_off_vix
    ):
        errors.append("VIX High Threshold 必须小于 VIX Risk-Off Threshold。")

    return errors
