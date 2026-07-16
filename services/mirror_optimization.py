from __future__ import annotations

from typing import Mapping


EXPECTED_METHODOLOGY = "expanding_walk_forward_with_untouched_holdout"


def optimization_result_error(
    result: Mapping[str, object],
    current_snapshot_id: int,
) -> str:
    """Return why a mirror optimization result must not be displayed as current."""
    if not result:
        return ""
    if (
        result.get("schema_version") != 2
        or result.get("methodology") != EXPECTED_METHODOLOGY
    ):
        return (
            "Legacy single-split optimization result is blocked. Rerun the "
            "strict walk-forward optimizer."
        )
    if result.get("mirror_snapshot_id") != current_snapshot_id:
        return (
            "Optimization result belongs to a different mirror snapshot and "
            "is blocked until the optimizer is rerun."
        )
    return ""
