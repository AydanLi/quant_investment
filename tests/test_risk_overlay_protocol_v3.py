import pytest

from config.settings import Config
from research.risk_overlay_protocol import (
    risk_overlay_candidate_grid,
    validate_overlay_stage,
)


def test_daily_overlay_is_disabled_until_separate_admission():
    assert Config().daily_regime_overlay_enabled is False
    with pytest.raises(ValueError, match="independent admission"):
        Config(daily_regime_overlay_enabled=True).validate_risk_constraints()

    labels = {candidate.label for candidate in risk_overlay_candidate_grid()}
    validate_overlay_stage(
        core_strategy_frozen=True,
        evaluated_labels=labels,
        selected_label="daily_baseline",
    )
