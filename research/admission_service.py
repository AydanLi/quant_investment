from __future__ import annotations

from typing import Mapping

from storage.repositories.governance import GovernanceRepository


def persist_nested_admission(
    repository: GovernanceRepository,
    *,
    strategy_version: str,
    result: Mapping[str, object],
    gates: Mapping[str, bool],
) -> int:
    if bool(result.get("selection_uses_future_holdout", True)):
        raise ValueError("Future holdout use forbids admission persistence.")
    final_trials = list(result.get("final_selection_trials", []))
    if len(final_trials) != 135:
        raise ValueError("All 135 final candidate results must be persisted.")
    payload = {**dict(result), "gates": dict(gates), "admitted": all(gates.values())}
    return repository.save_admission(
        strategy_version=strategy_version,
        methodology="nested_expanding_v3",
        status="admitted" if all(gates.values()) else "rejected",
        results=payload,
        trials=final_trials,
    )
