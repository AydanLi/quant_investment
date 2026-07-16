from pathlib import Path

import pandas as pd
import pytest

from services.dashboard_display import format_parameter_display_value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (3, "3"),
        (0.12, "0.12"),
        ("monthly", "monthly"),
        (None, ""),
        (float("nan"), ""),
        (pd.NA, ""),
        ({"risk_model": "ewma"}, "{'risk_model': 'ewma'}"),
        ([1, 2], "[1, 2]"),
    ],
)
def test_format_parameter_display_value_returns_arrow_safe_text(value, expected):
    result = format_parameter_display_value(value)

    assert result == expected
    assert isinstance(result, str)


def test_database_dashboards_do_not_use_deprecated_container_width_argument():
    project_root = Path(__file__).resolve().parents[1]
    dashboard_paths = [
        project_root / "streamlit_dashboard_db.py",
        project_root / "streamlit_dashboard_db_v1_1_save_experiment.py",
        project_root / "robinhood_mirror_dashboard.py",
    ]

    for dashboard_path in dashboard_paths:
        source = dashboard_path.read_text(encoding="utf-8")
        assert "use_container_width" not in source, dashboard_path.name
