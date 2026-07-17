from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskOverlayCandidate:
    label: str
    daily_regime_changes: bool
    vix_high: float
    vix_risk_off: float
    drawdown_200d: float


def risk_overlay_candidate_grid() -> tuple[RiskOverlayCandidate, ...]:
    return (
        RiskOverlayCandidate("monthly_only_baseline", False, 22.0, 28.0, -0.08),
        RiskOverlayCandidate("daily_defensive", True, 20.0, 25.0, -0.05),
        RiskOverlayCandidate("daily_baseline", True, 22.0, 28.0, -0.08),
        RiskOverlayCandidate("daily_slow", True, 25.0, 32.0, -0.10),
    )


def validate_overlay_stage(
    *,
    core_strategy_frozen: bool,
    evaluated_labels: set[str],
    selected_label: str,
) -> None:
    if not core_strategy_frozen:
        raise ValueError("Daily risk overlay cannot be evaluated before core freeze.")
    required = {candidate.label for candidate in risk_overlay_candidate_grid()}
    if evaluated_labels != required:
        raise ValueError("Every preregistered overlay candidate must be evaluated.")
    if selected_label not in required:
        raise ValueError("Selected overlay is outside the preregistered grid.")
