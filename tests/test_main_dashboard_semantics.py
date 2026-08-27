from pathlib import Path

from config.settings import Config
from streamlit_dashboard_db_v1_1_save_experiment import (
    dashboard_risk_model_options,
    frequency_governance_status,
    parse_admitted_dynamic_factor,
)


def test_formal_dashboard_defaults_to_governed_baseline():
    config = Config()
    options = dashboard_risk_model_options(None)

    assert config.risk_model == "sample"
    assert config.max_asset_weight == 0.35
    assert list(options) == ["Sample covariance（基准）"]
    assert options["Sample covariance（基准）"]["risk_model"] == "sample"

    source = (
        Path(__file__).resolve().parents[1]
        / "streamlit_dashboard_db_v1_1_save_experiment.py"
    ).read_text(encoding="utf-8")
    assert "max_value=baseline_config.max_asset_weight" in source
    assert "value=baseline_config.max_asset_weight" in source
    assert 'risk_model="dynamic_factor"' not in source


def test_dynamic_factor_option_requires_parseable_admitted_result():
    assert parse_admitted_dynamic_factor({}) is None
    payload = {
        "core_strategy_frozen": True,
        "baseline_model": "sample",
        "candidate_count": 6,
        "selected_admitted_candidate": "dynamic_half_life_40_stress_1.5",
        "evaluations": {
            "dynamic_half_life_40_stress_1.5": {"admitted": True}
        },
    }
    admitted = parse_admitted_dynamic_factor(payload)

    assert admitted == {
        "risk_model": "dynamic_factor",
        "ewma_half_life_days": 40,
        "pca_stress_multiplier": 1.5,
        "admission_label": "dynamic_half_life_40_stress_1.5",
    }
    options = dashboard_risk_model_options(admitted)
    assert len(options) == 2
    assert any("已准入" in label for label in options)

    payload["candidate_count"] = 7
    assert parse_admitted_dynamic_factor(payload) is None


def test_daily_and_weekly_frequencies_are_exploratory_only():
    assert frequency_governance_status("D") == "exploratory_only"
    assert frequency_governance_status("W") == "exploratory_only"
    assert frequency_governance_status("M") == "admission_candidate"
