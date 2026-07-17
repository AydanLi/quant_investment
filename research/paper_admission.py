from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

import numpy as np


@dataclass(frozen=True)
class PaperAdmissionEvidence:
    frozen_at: datetime
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
    elapsed_days = (evidence.evaluated_at - evidence.frozen_at).total_seconds() / 86_400
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
