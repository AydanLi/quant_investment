from datetime import datetime, timedelta, timezone

import pytest

from services.mirror_optimization import (
    normalize_cost_basis_weights,
    optimization_result_error,
)


NOW = datetime(2026, 7, 16, 4, 0, tzinfo=timezone.utc)
FINGERPRINT = "a" * 64


def _valid_result():
    gates = {
        "same_interval_same_costs": True,
        "holdout_sharpe": False,
    }
    return {
        "schema_version": 2,
        "methodology": "expanding_walk_forward_with_untouched_holdout",
        "generated_at": "2026-07-16T03:11:14+00:00",
        "source_fingerprint": FINGERPRINT,
        "mirror_snapshot_id": 7,
        "best": {
            "test_cagr": 0.12,
            "test_sharpe": 1.1,
            "test_max_drawdown": -0.08,
            "test_annual_turnover": 3.0,
        },
        "admission": {
            "selection_uses_holdout": False,
            "gates": gates,
            "admitted": False,
            "position_changes_authorized": False,
        },
        "admitted": False,
        "position_changes_authorized": False,
        "latest_signal_date": "2026-07-15",
        "latest_weights": {"BIL": 0.4, "SPY": 0.6},
    }


def _error(result, snapshot_id=7, **kwargs):
    return optimization_result_error(
        result,
        snapshot_id,
        expected_source_fingerprint=kwargs.pop(
            "expected_source_fingerprint", FINGERPRINT
        ),
        snapshot_captured_at=kwargs.pop(
            "snapshot_captured_at", "2026-07-15T21:11:16+00:00"
        ),
        now=kwargs.pop("now", NOW),
        **kwargs,
    )


def test_valid_result_matches_current_snapshot_and_source():
    assert _error(_valid_result()) == ""


def test_legacy_single_split_result_is_blocked():
    result = _valid_result()
    result.pop("schema_version")

    assert "Legacy single-split" in _error(result)


def test_non_object_and_old_snapshot_results_are_blocked():
    assert "JSON object" in _error([])
    assert "different mirror snapshot" in _error(_valid_result(), snapshot_id=8)


def test_missing_fields_and_source_changes_are_blocked():
    incomplete = _valid_result()
    incomplete.pop("best")
    changed = _valid_result()
    changed["source_fingerprint"] = "b" * 64

    assert "Missing: best" in _error(incomplete)
    assert "different source code" in _error(changed)


@pytest.mark.parametrize(
    ("generated_at", "expected"),
    [
        ("not-a-date", "generation time"),
        ("2026-07-16T03:11:14", "timezone-naive"),
        ("2026-07-15T20:00:00+00:00", "predates"),
        ("2026-07-16T05:00:00+00:00", "future"),
    ],
)
def test_invalid_or_stale_generation_times_are_blocked(generated_at, expected):
    result = _valid_result()
    result["generated_at"] = generated_at

    assert expected in _error(result)


def test_invalid_metrics_and_weights_are_blocked():
    invalid_metric = _valid_result()
    invalid_metric["best"]["test_sharpe"] = float("nan")
    invalid_weights = _valid_result()
    invalid_weights["latest_weights"] = {"BIL": 0.8, "SPY": 0.3}

    assert "invalid values" in _error(invalid_metric)
    assert "do not sum to 1" in _error(invalid_weights)


def test_inconsistent_admission_or_holdout_use_is_blocked():
    inconsistent = _valid_result()
    inconsistent["admitted"] = True
    holdout_leak = _valid_result()
    holdout_leak["admission"]["selection_uses_holdout"] = True

    assert "inconsistent" in _error(inconsistent)
    assert "holdout isolation" in _error(holdout_leak)


def test_result_age_boundary_is_accepted():
    result = _valid_result()
    result["generated_at"] = (NOW - timedelta(days=7)).isoformat()

    assert _error(
        result,
        snapshot_captured_at="2026-06-01T00:00:00+00:00",
    ) == ""


def test_naive_validation_clock_is_treated_as_utc():
    assert _error(_valid_result(), now=NOW.replace(tzinfo=None)) == ""


def test_invalid_snapshot_capture_time_is_blocked():
    assert "snapshot capture time" in _error(
        _valid_result(),
        snapshot_captured_at="not-a-date",
    )


def test_result_older_than_freshness_window_is_blocked():
    result = _valid_result()
    result["generated_at"] = "2026-07-01T00:00:00+00:00"

    assert "older" in _error(
        result,
        snapshot_captured_at="2026-06-01T00:00:00+00:00",
    )


def test_cost_basis_weights_are_normalized_or_safely_zeroed():
    weights, total, valid = normalize_cost_basis_weights([100.0, 300.0])
    assert weights == [0.25, 0.75]
    assert total == 400.0
    assert valid is True

    zero_weights, zero_total, zero_valid = normalize_cost_basis_weights([0.0, 0.0])
    assert zero_weights == [0.0, 0.0]
    assert zero_total == 0.0
    assert zero_valid is False

    invalid_weights, _, invalid_valid = normalize_cost_basis_weights(
        [100.0, float("inf")]
    )
    assert invalid_weights == [0.0, 0.0]
    assert invalid_valid is False
