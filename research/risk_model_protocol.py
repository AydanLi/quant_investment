from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskModelCandidate:
    label: str
    half_life_days: int
    stress_multiplier: float


def dynamic_factor_candidate_grid() -> tuple[RiskModelCandidate, ...]:
    return tuple(
        RiskModelCandidate(
            label=f"dynamic_half_life_{half_life}_stress_{stress:.1f}",
            half_life_days=half_life,
            stress_multiplier=stress,
        )
        for half_life in (20, 40, 60)
        for stress in (1.0, 1.5)
    )


def validate_risk_model_stage(
    *,
    core_strategy_frozen: bool,
    baseline_model: str,
    evaluated_labels: set[str],
) -> None:
    if not core_strategy_frozen:
        raise ValueError("Risk-model admission starts only after the core strategy is frozen.")
    if baseline_model != "sample":
        raise ValueError("Sample covariance must be the risk-model baseline.")
    required = {candidate.label for candidate in dynamic_factor_candidate_grid()}
    if evaluated_labels != required:
        missing = sorted(required - evaluated_labels)
        extra = sorted(evaluated_labels - required)
        raise ValueError(f"Risk-model grid mismatch; missing={missing}, extra={extra}.")
