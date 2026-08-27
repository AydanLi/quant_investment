from __future__ import annotations

from typing import Mapping

from storage.repositories.governance import GovernanceRepository


def build_nested_admission_payload(
    *,
    result: Mapping[str, object],
    gates: Mapping[str, bool],
) -> tuple[dict[str, object], list[Mapping[str, object]], bool]:
    if bool(result.get("selection_uses_future_holdout", True)):
        raise ValueError("Future holdout use forbids admission persistence.")
    final_trials = list(result.get("final_selection_trials", []))
    if len(final_trials) != 135 or len(
        {str(item.get("label")) for item in final_trials}
    ) != 135:
        raise ValueError("All 135 unique final candidate results must be persisted.")
    trials_valid = all(item.get("status") == "evaluated" for item in final_trials)
    admitted = bool(trials_valid and all(gates.values()))
    payload = {**dict(result), "gates": dict(gates), "admitted": admitted}
    return payload, final_trials, admitted


def persist_nested_admission(
    repository: GovernanceRepository,
    *,
    strategy_version: str,
    result: Mapping[str, object],
    gates: Mapping[str, bool],
) -> int:
    payload, final_trials, admitted = build_nested_admission_payload(
        result=result,
        gates=gates,
    )
    admission_id = repository.start_admission(
        strategy_version=strategy_version,
        methodology="nested_expanding_v3",
        results={
            "protocol_hash": result.get("protocol_hash"),
            "selection_uses_future_holdout": False,
        },
    )
    for trial in final_trials:
        repository.save_admission_trial(
            admission_id,
            stage="final_selection_summary",
            fold_key="aggregate",
            label=str(trial["label"]),
            parameters=trial.get("parameters", {}),
            folds=trial.get("folds", []),
            status=str(trial.get("status", "evaluated")),
            score=trial.get("score"),
        )
    repository.finish_admission(
        admission_id,
        status="admitted" if admitted else "rejected",
        results=payload,
    )
    return admission_id
