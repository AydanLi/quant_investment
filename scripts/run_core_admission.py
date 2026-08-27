from __future__ import annotations

import argparse
from inspect import signature
import json
from pathlib import Path
import sys
from typing import Callable, Mapping

import numpy as np
import pandas as pd
from sqlalchemy import select

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.settings import Config
from data.adjustments import locally_adjust_ohlcv
from data.quality import raw_market_data_hash
from research.admission_service import build_nested_admission_payload
from research.core_evaluator import CoreStrategyEvaluator
from research.nested_walk_forward import (
    NestedExpandingAdmissionRunner,
    fixed_current_baseline_candidate,
    historical_admission_gates,
)
from research.protocol import ResearchProtocol, build_protocol
from storage.db import create_db_engine
from storage.repositories.governance import GovernanceRepository
from storage.repositories.trusted_data import TrustedMarketDataRepository
from storage.schema import admission_runs, dataset_snapshots, parameter_trials


METHODOLOGY = "nested_expanding_v3"


def _database_url(database: str) -> str:
    return database if "://" in database else f"sqlite:///{Path(database).as_posix()}"


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def load_protocol(path: Path) -> ResearchProtocol:
    payload = json.loads(path.read_text(encoding="utf-8"))
    stored_hash = str(payload.pop("content_hash", ""))
    protocol = build_protocol(
        protocol_version=str(payload.get("protocol_version", "")),
        code_commit=str(payload.get("code_commit", "")),
        dataset_snapshot_id=int(payload.get("dataset_snapshot_id", 0)),
        universe_version=str(payload.get("universe_version", "")),
    )
    expected = json.loads(json.dumps(protocol.to_dict(), sort_keys=True))
    if payload != expected or stored_hash != protocol.content_hash:
        raise ValueError("Protocol content or hash does not match the fixed 135-candidate protocol.")
    return protocol


def load_immutable_snapshot(
    database: str,
    snapshot_id: int,
) -> dict[str, pd.DataFrame]:
    engine = create_db_engine(_database_url(database))
    with engine.connect() as connection:
        snapshot = connection.execute(
            select(
                dataset_snapshots.c.id,
                dataset_snapshots.c.status,
                dataset_snapshots.c.content_hash,
                dataset_snapshots.c.decision_set_hash,
                dataset_snapshots.c.quality_json,
            ).where(dataset_snapshots.c.id == snapshot_id)
        ).one_or_none()
    if snapshot is None:
        raise ValueError(f"Dataset snapshot {snapshot_id} does not exist.")
    if snapshot.status not in {"TRUSTED", "TRUSTED_WITH_EXCEPTIONS"}:
        raise ValueError(
            f"Dataset snapshot {snapshot_id} is {snapshot.status}, not trusted for admission."
        )
    if not snapshot.content_hash:
        raise ValueError("Admission requires an immutable snapshot content hash.")
    quality = dict(snapshot.quality_json or {})
    if quality.get("stale_sessions") != 0:
        raise ValueError("Core admission requires a zero-staleness dataset snapshot.")
    if quality.get("content_hash") != snapshot.content_hash:
        raise ValueError("Snapshot quality metadata does not match its stored content hash.")
    if snapshot.status == "TRUSTED_WITH_EXCEPTIONS" and (
        not snapshot.decision_set_hash
        or quality.get("decision_set_hash") != snapshot.decision_set_hash
    ):
        raise ValueError(
            "TRUSTED_WITH_EXCEPTIONS requires a matching decision_set_hash."
        )

    trusted = TrustedMarketDataRepository(engine=engine)
    sources = trusted.load_snapshot_sources(snapshot_id)
    payload = sources.get("primary")
    if payload is None:
        raise ValueError("Immutable snapshot contains no primary market bars.")
    actual_raw_hash = raw_market_data_hash(payload, sources.get("secondary"))
    if not quality.get("raw_data_hash") or actual_raw_hash != quality["raw_data_hash"]:
        raise ValueError("Snapshot bars/actions do not match the stored raw_data_hash.")
    actions_by_ticker: dict[str, list[object]] = {}
    for action in payload.actions:
        actions_by_ticker.setdefault(action.ticker, []).append(action)
    data = {
        ticker: locally_adjust_ohlcv(frame, actions_by_ticker.get(ticker, ()))
        for ticker, frame in payload.bars.items()
    }
    if not data:
        raise ValueError("Immutable snapshot contains no primary market bars.")
    return data


def _governance_call(repository: object, method: str, **kwargs: object) -> object:
    function = getattr(repository, method, None)
    if function is None:
        raise RuntimeError(
            f"GovernanceRepository.{method} is required by core admission."
        )
    return function(**kwargs)


def _start_admission(
    repository: object,
    *,
    strategy_version: str,
    protocol_hash: str,
) -> int:
    function = getattr(repository, "start_admission", None)
    if function is None:
        raise RuntimeError(
            "GovernanceRepository.start_admission is required by core admission."
        )
    parameters = signature(function).parameters
    kwargs: dict[str, object] = {
        "strategy_version": strategy_version,
        "methodology": METHODOLOGY,
    }
    if "protocol_hash" in parameters:
        kwargs["protocol_hash"] = protocol_hash
    if "results" in parameters:
        kwargs["results"] = {
            "protocol_hash": protocol_hash,
            "selection_uses_future_holdout": False,
        }
    return int(function(**kwargs))


def _finish_admission(
    repository: object,
    *,
    admission_run_id: int,
    status: str,
    results: Mapping[str, object],
    error_message: str | None = None,
) -> None:
    function = getattr(repository, "finish_admission", None)
    if function is None:
        raise RuntimeError(
            "GovernanceRepository.finish_admission is required by core admission."
        )
    kwargs: dict[str, object] = {
        "admission_run_id": admission_run_id,
        "status": status,
        "results": results,
    }
    if "error_message" in signature(function).parameters:
        kwargs["error_message"] = error_message
    function(**kwargs)


def _latest_admission(repository: object, strategy_version: str):
    engine = getattr(repository, "engine", None)
    if engine is None:
        return None
    with engine.connect() as connection:
        return connection.execute(
            select(admission_runs)
            .where(
                admission_runs.c.strategy_version == strategy_version,
                admission_runs.c.methodology == METHODOLOGY,
            )
            .order_by(admission_runs.c.id.desc())
            .limit(1)
        ).mappings().one_or_none()


def _trial_cache(
    repository: object,
    admission_run_id: int,
) -> dict[tuple[str, str, str], dict[str, object]]:
    engine = getattr(repository, "engine", None)
    if engine is None:
        return {}
    with engine.connect() as connection:
        rows = connection.execute(
            select(parameter_trials).where(
                parameter_trials.c.admission_run_id == admission_run_id,
                parameter_trials.c.stage != "final_selection_summary",
            )
        ).mappings().all()
    cache: dict[tuple[str, str, str], dict[str, object]] = {}
    for row in rows:
        folds = list(row["folds_json"] or [])
        if len(folds) != 1 or not isinstance(folds[0], Mapping):
            raise ValueError("Persisted admission trial has malformed fold data.")
        saved = dict(folds[0])
        cache[(str(row["stage"]), str(row["fold_key"]), str(row["label"]))] = {
            "status": str(saved.get("status", row["status"])),
            "metrics": saved.get("metrics"),
            **({"error": saved["error"]} if "error" in saved else {}),
        }
    return cache


def _compound_outer(
    outer_results: list[dict[str, object]],
    cost_bps: float,
    metric: str,
) -> float:
    values = [
        float(item["cost_scenarios"][str(cost_bps)][metric])
        for item in outer_results
    ]
    if not values or any(not np.isfinite(value) or value <= -1.0 for value in values):
        return float("-inf")
    return float(np.prod(np.asarray(values) + 1.0) - 1.0)


def _elapsed_outer_years(outer_results: list[dict[str, object]]) -> float:
    days = sum(
        (
            pd.Timestamp(item["fold"]["validation_end"])
            - pd.Timestamp(item["fold"]["validation_start"])
        ).days
        + 1
        for item in outer_results
    )
    return days / 365.2425


def _replacement_hurdle_metrics(
    result: Mapping[str, object],
    protocol: ResearchProtocol,
) -> dict[str, object]:
    expected = fixed_current_baseline_candidate(protocol)
    if result.get("replacement_baseline_label") != expected.label:
        raise ValueError("Replacement baseline does not match the fixed protocol candidate.")

    outer_results = list(result.get("outer_folds", []))
    baseline_results = list(result.get("replacement_baseline_folds", []))
    if not outer_results or len(outer_results) != len(baseline_results):
        raise ValueError("Every outer fold requires a paired replacement baseline fold.")

    candidate_sharpes: list[float] = []
    baseline_sharpes: list[float] = []
    candidate_drawdowns: list[float] = []
    baseline_drawdowns: list[float] = []
    candidate_valid = True
    for candidate_fold, baseline_fold in zip(outer_results, baseline_results):
        if (
            candidate_fold.get("outer_fold") != baseline_fold.get("outer_fold")
            or candidate_fold.get("fold") != baseline_fold.get("fold")
            or baseline_fold.get("baseline_label") != expected.label
        ):
            raise ValueError("Replacement baseline folds are not aligned to outer folds.")
        candidate = candidate_fold["cost_scenarios"]["7.0"]
        baseline = baseline_fold["cost_scenarios"]["7.0"]
        if baseline.get("evaluation_status") != "evaluated" or bool(
            baseline.get("degenerate_all_cash", False)
        ):
            raise ValueError("Fixed replacement baseline evaluation is not usable.")
        baseline_values = (
            float(baseline["excess_sharpe"]),
            float(baseline["max_drawdown"]),
        )
        if not all(np.isfinite(value) for value in baseline_values):
            raise ValueError("Fixed replacement baseline metrics must be finite.")
        baseline_sharpes.append(baseline_values[0])
        baseline_drawdowns.append(baseline_values[1])

        if candidate.get("evaluation_status") != "evaluated" or bool(
            candidate.get("degenerate_all_cash", False)
        ):
            candidate_valid = False
            continue
        candidate_values = (
            float(candidate["excess_sharpe"]),
            float(candidate["max_drawdown"]),
        )
        if not all(np.isfinite(value) for value in candidate_values):
            candidate_valid = False
            continue
        candidate_sharpes.append(candidate_values[0])
        candidate_drawdowns.append(candidate_values[1])

    candidate_median_sharpe = (
        float(np.median(candidate_sharpes))
        if candidate_valid and len(candidate_sharpes) == len(outer_results)
        else float("-inf")
    )
    baseline_median_sharpe = float(np.median(baseline_sharpes))
    candidate_worst_drawdown = (
        float(min(candidate_drawdowns))
        if candidate_valid and len(candidate_drawdowns) == len(outer_results)
        else float("nan")
    )
    baseline_worst_drawdown = float(min(baseline_drawdowns))
    baseline_drawdown_depth = abs(baseline_worst_drawdown)
    drawdown_improvement = (
        (baseline_drawdown_depth - abs(candidate_worst_drawdown))
        / baseline_drawdown_depth
        if candidate_valid and baseline_drawdown_depth > 0.0
        else float("-inf")
    )
    return {
        "baseline_label": expected.label,
        "cost_bps": 7.0,
        "paired_outer_windows": len(outer_results),
        "candidate_evaluations_valid": candidate_valid,
        "candidate_median_excess_sharpe": candidate_median_sharpe,
        "baseline_median_excess_sharpe": baseline_median_sharpe,
        "excess_sharpe_improvement": (
            candidate_median_sharpe - baseline_median_sharpe
        ),
        "candidate_worst_max_drawdown": candidate_worst_drawdown,
        "baseline_worst_max_drawdown": baseline_worst_drawdown,
        "drawdown_improvement": drawdown_improvement,
    }


def execute_core_admission(
    *,
    protocol: ResearchProtocol,
    strategy_version: str,
    data: Mapping[str, pd.DataFrame],
    repository: object,
    evaluator: Callable | None = None,
) -> dict[str, object]:
    if not strategy_version.strip():
        raise ValueError("strategy_version is required.")
    _governance_call(
        repository,
        "create_strategy_version",
        version=strategy_version,
        universe_version=protocol.universe_version,
        dataset_snapshot_id=protocol.dataset_snapshot_id,
        code_commit=protocol.code_commit,
        protocol=protocol.to_dict(),
    )
    existing = _latest_admission(repository, strategy_version)
    if existing is not None and str(existing["status"]).lower() in {
        "admitted",
        "rejected",
        "failed",
    }:
        stored = dict(existing["results_json"] or {})
        if stored.get("protocol_hash") != protocol.content_hash:
            raise ValueError("Terminal AdmissionRun protocol hash does not match the strategy.")
        status = str(existing["status"]).upper()
        if status == "ADMITTED":
            gates = dict(stored.get("gates") or {})
            comparison = dict(stored.get("replacement_comparison") or {})
            if not all(
                gates.get(name) is True
                for name in ("replacement_sharpe", "replacement_drawdown")
            ) or comparison.get("baseline_label") != fixed_current_baseline_candidate(
                protocol
            ).label:
                raise ValueError(
                    "Terminal AdmissionRun lacks mandatory fixed-baseline evidence."
                )
            _governance_call(
                repository,
                "freeze_strategy_version",
                version=strategy_version,
                admission_run_id=int(existing["id"]),
            )
            _governance_call(
                repository,
                "start_local_sim_clock",
                version=strategy_version,
            )
        return {
            "admission_run_id": int(existing["id"]),
            "strategy_version": strategy_version,
            "status": status,
            "final_selected_label": stored.get("final_selected_label"),
            "gates": stored.get("gates", {}),
            "reused_terminal_run": True,
        }

    admission_run_id = _start_admission(
        repository,
        strategy_version=strategy_version,
        protocol_hash=protocol.content_hash,
    )

    def persist_trial(trial: Mapping[str, object]) -> None:
        _governance_call(
            repository,
            "save_admission_trial",
            admission_run_id=admission_run_id,
            stage=str(trial["stage"]),
            fold_key=str(trial["fold_key"]),
            label=str(trial["label"]),
            parameters=trial["parameters"],
            folds=[
                {
                    "cost_bps": trial["cost_bps"],
                    "status": trial["status"],
                    "metrics": trial["metrics"],
                    **({"error": trial["error"]} if "error" in trial else {}),
                }
            ],
            status=str(trial["status"]),
            score=float(trial["score"]),
        )

    runner = NestedExpandingAdmissionRunner(
        protocol,
        evaluator or CoreStrategyEvaluator(Config()),
        trial_callback=persist_trial,
        trial_cache=_trial_cache(repository, admission_run_id),
    )
    finished = False
    try:
        raw_result = runner.run(data)
        replacement = _replacement_hurdle_metrics(raw_result, protocol)
        result = {**raw_result, "replacement_comparison": replacement}
        outer_results = list(result["outer_folds"])
        robustness = result["robustness"]
        gates = historical_admission_gates(
            outer_results,
            protocol,
            aggregate_net_return=_compound_outer(outer_results, 7.0, "net_return"),
            aggregate_bil_return=_compound_outer(
                outer_results, 7.0, "benchmark_return"
            ),
            neighbor_pass_rate=float(robustness["neighbor_pass_rate"]),
            start_date_pass_rate=float(robustness["start_date_pass_rate"]),
            elapsed_years=_elapsed_outer_years(outer_results),
            replacement_excess_sharpe_improvement=float(
                replacement["excess_sharpe_improvement"]
            ),
            replacement_drawdown_improvement=float(
                replacement["drawdown_improvement"]
            ),
        )
        payload, final_trials, admitted = build_nested_admission_payload(
            result=result,
            gates=gates,
        )
        for trial in final_trials:
            _governance_call(
                repository,
                "save_admission_trial",
                admission_run_id=admission_run_id,
                stage="final_selection_summary",
                fold_key="aggregate",
                label=str(trial["label"]),
                parameters=trial["parameters"],
                folds=trial["folds"],
                status=str(trial["status"]),
                score=float(trial["score"]),
            )
        _finish_admission(
            repository,
            admission_run_id=admission_run_id,
            status="ADMITTED" if admitted else "REJECTED",
            results=_jsonable(payload),
        )
        finished = True
        if admitted:
            _governance_call(
                repository,
                "freeze_strategy_version",
                version=strategy_version,
                admission_run_id=admission_run_id,
            )
            _governance_call(
                repository,
                "start_local_sim_clock",
                version=strategy_version,
            )
        return {
            "admission_run_id": admission_run_id,
            "strategy_version": strategy_version,
            "status": "ADMITTED" if admitted else "REJECTED",
            "final_selected_label": result["final_selected_label"],
            "gates": gates,
        }
    except Exception as exc:
        if not finished:
            error = f"{type(exc).__name__}: {exc}"
            _finish_admission(
                repository,
                admission_run_id=admission_run_id,
                status="FAILED",
                results={
                    "protocol_hash": protocol.content_hash,
                    "selection_uses_future_holdout": False,
                    "error": error,
                },
                error_message=error,
            )
        raise


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the fixed 135-candidate core admission on an immutable snapshot."
    )
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--strategy-version", required=True)
    parser.add_argument("--database", default="quant_research.db")
    args = parser.parse_args()

    protocol = load_protocol(args.protocol)
    data = load_immutable_snapshot(args.database, protocol.dataset_snapshot_id)
    engine = create_db_engine(_database_url(args.database))
    result = execute_core_admission(
        protocol=protocol,
        strategy_version=args.strategy_version,
        data=data,
        repository=GovernanceRepository(engine=engine),
    )
    print(json.dumps(_jsonable(result), indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
