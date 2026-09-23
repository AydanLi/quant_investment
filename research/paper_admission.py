from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import numpy as np


@dataclass(frozen=True)
class PaperAdmissionEvidence:
    started_at: datetime
    evaluated_at: datetime
    completed_rebalances: int
    fill_count: int
    implementation_shortfall_bps: tuple[float, ...]
    unauthorized_order_count: int
    duplicate_order_count: int
    unresolved_reconciliation_incidents: int
    drawdown_stop_count: int
    unexplained_shadow_differences: int


def evaluate_paper_admission(evidence: PaperAdmissionEvidence) -> dict[str, object]:
    elapsed_days = (evidence.evaluated_at - evidence.started_at).total_seconds() / 86_400
    deviations = np.abs(np.asarray(evidence.implementation_shortfall_bps, dtype=float))
    median = float(np.median(deviations)) if len(deviations) else float("nan")
    percentile_95 = float(np.percentile(deviations, 95)) if len(deviations) else float("nan")
    gates = {
        "twelve_months": elapsed_days >= 365.0,
        "twelve_rebalances": evidence.completed_rebalances >= 12,
        "thirty_fills": evidence.fill_count >= 30 and len(deviations) >= 30,
        "authorized_and_idempotent": evidence.unauthorized_order_count == 0 and evidence.duplicate_order_count == 0,
        "reconciled": evidence.unresolved_reconciliation_incidents == 0,
        "median_shortfall": np.isfinite(median) and median <= 7.0,
        "p95_shortfall": np.isfinite(percentile_95) and percentile_95 <= 20.0,
        "no_drawdown_stop": evidence.drawdown_stop_count == 0,
        "shadow_explained": evidence.unexplained_shadow_differences == 0,
    }
    return {
        "admitted": all(gates.values()),
        "gates": gates,
        "elapsed_days": elapsed_days,
        "median_implementation_shortfall_bps": median,
        "p95_implementation_shortfall_bps": percentile_95,
        "evidence": asdict(evidence),
    }


def evaluate_persisted_paper_admission(engine, validation_run_id: int, *, evaluated_at=None) -> dict[str, object]:
    """Read prospective persisted evidence; historical replay cannot age the clock.

    REPLAY_OPEN observations establish local operational simulation only. A broker
    observation stage needs a separate connector/provenance contract before this
    evaluator can ever produce a broker or live admission.
    """
    from sqlalchemy import select
    from storage.schema import (validation_runs, strategy_versions, signal_decisions,
                                paper_cycles, order_intents, execution_fills,
                                reconciliations, risk_incidents)

    def utc_naive(value):
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    end = utc_naive(evaluated_at) if evaluated_at is not None else now
    if end > now:
        raise ValueError("Paper evidence cannot be evaluated at a future timestamp.")
    with engine.connect() as conn:
        run = conn.execute(select(validation_runs).where(
            validation_runs.c.id == validation_run_id)).mappings().one_or_none()
        if run is None:
            raise ValueError("Unknown validation run.")
        strategy = conn.execute(select(strategy_versions).where(
            strategy_versions.c.version == run["strategy_version"])).mappings().one()
        start = utc_naive(run["started_at"])
        if run["ended_at"] is not None:
            end = min(end, utc_naive(run["ended_at"]))
        if end < start:
            raise ValueError("Evaluation precedes the validation stage start.")
        signals = conn.execute(select(signal_decisions).where(
            signal_decisions.c.strategy_version == run["strategy_version"],
            signal_decisions.c.environment == run["environment"],
            signal_decisions.c.runtime_hash == run["runtime_hash"],
            signal_decisions.c.generated_at >= start,
            signal_decisions.c.generated_at <= end,
            signal_decisions.c.recorded_at >= start,
            signal_decisions.c.recorded_at <= end,
        )).mappings().all()
        signals = [row for row in signals if str(row["signal_session"]) >= str(start.date())]
        signal_ids = {int(row["id"]) for row in signals}
        cycles = conn.execute(select(paper_cycles).where(
            paper_cycles.c.signal_decision_id.in_(signal_ids),
            paper_cycles.c.created_at >= start, paper_cycles.c.created_at <= end,
        )).mappings().all()
        cycle_ids = {int(row["id"]) for row in cycles}
        orders = conn.execute(select(order_intents).where(
            order_intents.c.paper_cycle_id.in_(cycle_ids),
            order_intents.c.environment == run["environment"],
        )).mappings().all()
        orders = [row for row in orders if
                  (row["metadata_json"] or {}).get("account_before", {}).get("account_ref") == run["account_ref"]]
        order_map = {int(row["id"]): row for row in orders}
        fills = conn.execute(select(execution_fills).where(
            execution_fills.c.order_intent_id.in_(order_map),
            execution_fills.c.environment == run["environment"],
            execution_fills.c.filled_at >= start, execution_fills.c.filled_at <= end,
            execution_fills.c.recorded_at >= start, execution_fills.c.recorded_at <= end,
        )).mappings().all()
        reconciliation_rows = conn.execute(select(reconciliations).where(
            reconciliations.c.paper_cycle_id.in_(cycle_ids),
            reconciliations.c.account_ref == run["account_ref"],
            reconciliations.c.environment == run["environment"],
            reconciliations.c.created_at >= start, reconciliations.c.created_at <= end,
        )).mappings().all()
        incidents = conn.execute(select(risk_incidents).where(
            risk_incidents.c.strategy_version == run["strategy_version"],
            risk_incidents.c.account_ref == run["account_ref"],
            risk_incidents.c.environment == run["environment"],
            risk_incidents.c.created_at >= start, risk_incidents.c.created_at <= end,
        )).mappings().all()
    # Last reconciliation per cycle controls completion, preserving prior
    # failures as incidents instead of counting every rerun as a rebalance.
    latest = {}
    for row in sorted(reconciliation_rows, key=lambda row: row["id"]):
        latest[row["paper_cycle_id"]] = row
    cycle_map = {row["id"]: row for row in cycles}
    excluded_fills = 0
    prospective_fills = []
    for fill in fills:
        order = order_map[fill["order_intent_id"]]
        cycle = cycle_map[order["paper_cycle_id"]]
        provenance = (cycle["execution_payload_json"] or {}).get("REPLAY_OPEN", {})
        snapshot_as_of = utc_naive(provenance.get("snapshot_as_of"))
        if (not provenance.get("source_snapshot_id") or snapshot_as_of is None
                or snapshot_as_of > utc_naive(fill["filled_at"])):
            excluded_fills += 1
            continue
        prospective_fills.append(fill)
    fills = prospective_fills
    completed = [row for row in cycles if row["status"] == "COMPLETED"
                 and row["id"] in latest and latest[row["id"]]["status"] == "matched"]
    months = {str(row["signal_session"])[:7] for row in completed}
    deviations = tuple(float(row["implementation_shortfall_bps"]) for row in fills
                       if row["implementation_shortfall_bps"] is not None)
    unauthorized = sum(not order_map[row["order_intent_id"]]["approved_by"]
                       or order_map[row["order_intent_id"]]["approved_at"] is None
                       or utc_naive(order_map[row["order_intent_id"]]["approved_at"]) > utc_naive(row["filled_at"])
                       for row in fills)
    ids = [row["broker_execution_id"] for row in fills]
    unresolved = sum(str(row["status"]).lower() != "resolved" for row in incidents)
    evidence = PaperAdmissionEvidence(
        started_at=start, evaluated_at=end, completed_rebalances=len(months),
        fill_count=len(fills), implementation_shortfall_bps=deviations,
        unauthorized_order_count=unauthorized,
        duplicate_order_count=len(ids) - len(set(ids)),
        unresolved_reconciliation_incidents=unresolved + sum(row["status"] != "matched" for row in latest.values()),
        drawdown_stop_count=sum("DRAWDOWN" in str(row["code"]).upper() for row in incidents),
        unexplained_shadow_differences=sum(int((row["details_json"] or {}).get("unexplained_shadow_differences", 0)) for row in latest.values()),
    )
    result = evaluate_paper_admission(evidence)
    result["gates"].update(
        runtime_bound=strategy["runtime_hash"] == run["runtime_hash"],
        supported_execution=run["environment"] == "PAPER" and run["execution_model"] == "REPLAY_OPEN",
        fill_provenance=all(str(order_map[row["order_intent_id"]]["order_type"]).startswith("REPLAY_OPEN") for row in fills),
        shadow_observed=bool(completed) and all((latest[row["id"]]["details_json"] or {}).get("shadow_checked") is True for row in completed),
    )
    local_passed = all(result["gates"].values())
    return {**result, "admitted": False, "local_simulation_supported": local_passed,
            "broker_paper_admitted": False, "live_admitted": False,
            "status": "LOCAL_SIMULATION_SUPPORTED" if local_passed else "INSUFFICIENT_EVIDENCE",
            "validation_run_id": validation_run_id, "runtime_hash": run["runtime_hash"],
            "execution_model": run["execution_model"],
            "excluded_retrospective_fills": excluded_fills,
            "limitation": "REPLAY_OPEN evidence cannot establish broker-paper or live readiness."}
