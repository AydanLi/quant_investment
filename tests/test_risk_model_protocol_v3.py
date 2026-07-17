import pytest

from research.risk_model_protocol import (
    dynamic_factor_candidate_grid,
    validate_risk_model_stage,
)


def test_dynamic_risk_model_grid_is_exactly_six_preregistered_candidates():
    grid = dynamic_factor_candidate_grid()
    assert len(grid) == 6
    assert {item.half_life_days for item in grid} == {20, 40, 60}
    assert {item.stress_multiplier for item in grid} == {1.0, 1.5}


def test_risk_model_stage_requires_frozen_core_sample_baseline_and_complete_grid():
    labels = {item.label for item in dynamic_factor_candidate_grid()}
    validate_risk_model_stage(
        core_strategy_frozen=True,
        baseline_model="sample",
        evaluated_labels=labels,
    )
    with pytest.raises(ValueError, match="core strategy"):
        validate_risk_model_stage(
            core_strategy_frozen=False,
            baseline_model="sample",
            evaluated_labels=labels,
        )
    with pytest.raises(ValueError, match="grid mismatch"):
        validate_risk_model_stage(
            core_strategy_frozen=True,
            baseline_model="sample",
            evaluated_labels=set(),
        )
