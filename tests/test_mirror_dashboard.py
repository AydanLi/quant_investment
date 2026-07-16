import pandas as pd
import pytest

from services.mirror_dashboard import (
    build_admission_gate_table,
    build_allocation_comparison,
    format_result_age,
    format_timestamp_utc,
)


def test_allocation_comparison_uses_union_and_diagnostic_deltas():
    positions = pd.DataFrame(
        {
            "symbol": ["SPY", "QQQ"],
            "cost_basis_weight": [0.60, 0.40],
        }
    )

    result = build_allocation_comparison(
        positions,
        {"SPY": 0.20, "BIL": 0.80},
    ).set_index("symbol")

    assert set(result.index) == {"SPY", "QQQ", "BIL"}
    assert result.at["SPY", "diagnostic_delta"] == pytest.approx(-0.40)
    assert result.at["QQQ", "diagnostic_delta"] == pytest.approx(-0.40)
    assert result.at["BIL", "diagnostic_delta"] == pytest.approx(0.80)
    assert result.at["BIL", "absolute_delta"] == pytest.approx(0.80)


def test_admission_gate_table_has_stable_labels_and_status():
    table = build_admission_gate_table(
        {
            "gates": {
                "same_interval_same_costs": True,
                "rolling_windows": False,
                "custom_gate": False,
            }
        }
    )

    assert table["gate_key"].tolist() == [
        "same_interval_same_costs",
        "rolling_windows",
        "custom_gate",
    ]
    assert table["status"].tolist() == ["PASS", "FAIL", "FAIL"]
    assert table.iloc[0]["gate"] == "Same interval and costs"
    assert table.iloc[2]["gate"] == "Custom Gate"


@pytest.mark.parametrize(
    ("generated_at", "now", "expected"),
    [
        ("2026-07-16T03:11:00+00:00", "2026-07-16T03:41:00Z", "30m"),
        ("2026-07-16T03:11:00Z", "2026-07-16T05:26:00Z", "2h 15m"),
        ("2026-07-15T03:11:00Z", "2026-07-16T05:11:00Z", "1d 2h"),
    ],
)
def test_result_age_is_compact_and_utc_safe(generated_at, now, expected):
    assert format_result_age(generated_at, now=now) == expected


def test_timestamp_formatter_treats_sqlite_naive_value_as_utc():
    assert format_timestamp_utc("2026-07-15 21:11:16") == (
        "2026-07-15 21:11 UTC"
    )
