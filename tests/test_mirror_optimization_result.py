from services.mirror_optimization import optimization_result_error


def _valid_result():
    return {
        "schema_version": 2,
        "methodology": "expanding_walk_forward_with_untouched_holdout",
        "mirror_snapshot_id": 7,
    }


def test_valid_result_matches_current_snapshot():
    assert optimization_result_error(_valid_result(), 7) == ""


def test_legacy_single_split_result_is_blocked():
    result = _valid_result()
    result.pop("schema_version")

    assert "Legacy single-split" in optimization_result_error(result, 7)


def test_result_for_old_snapshot_is_blocked():
    assert "different mirror snapshot" in optimization_result_error(
        _valid_result(),
        8,
    )
