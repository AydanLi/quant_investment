from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
from typing import Iterable

from config.settings import Config


@dataclass(frozen=True)
class MomentumProfile:
    name: str
    weight_mom_20: float
    weight_mom_60: float
    weight_mom_120: float
    weight_low_vol: float


@dataclass(frozen=True)
class RegimeProfile:
    name: str
    vix_high_threshold: float
    vix_risk_off_threshold: float
    max_drawdown_from_200d: float
    risk_off_cash_weight: float


@dataclass(frozen=True)
class CandidateParameters:
    label: str
    momentum: MomentumProfile
    top_n: int
    target_annual_vol: float
    regime: RegimeProfile

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class AdmissionThresholdsV3:
    minimum_median_excess_sharpe: float = 0.0
    minimum_positive_outer_window_rate: float = 0.60
    minimum_neighbor_pass_rate: float = 0.67
    minimum_start_date_pass_rate: float = 0.67
    maximum_stop_overshoot: float = 0.02
    maximum_stops_per_five_years: float = 1.0
    replacement_excess_sharpe_improvement: float = 0.05
    replacement_drawdown_improvement: float = 0.10


@dataclass(frozen=True)
class ResearchProtocol:
    protocol_version: str
    code_commit: str
    dataset_snapshot_id: int
    universe_version: str
    benchmark_names: tuple[str, ...]
    cost_scenarios_bps: tuple[float, ...]
    outer_test_months: int
    minimum_outer_training_years: int
    candidates: tuple[CandidateParameters, ...]
    start_date_offsets_months: tuple[int, ...] = (0, 3, 6)
    thresholds: AdmissionThresholdsV3 = AdmissionThresholdsV3()
    rebalance_frequency: str = "M"
    execution_lag_sessions: int = 1
    minimum_weight: float = 0.10
    maximum_weight: float = 0.35
    frozen: bool = True

    def __post_init__(self) -> None:
        if len(self.candidates) != 135:
            raise ValueError("The admitted core protocol must contain exactly 135 candidates.")
        if self.rebalance_frequency != "M" or self.execution_lag_sessions != 1:
            raise ValueError("Admitted research is fixed to monthly T+1 execution.")
        if tuple(self.cost_scenarios_bps) != (2.0, 7.0, 20.0):
            raise ValueError("Admitted cost scenarios must remain 2/7/20 bps.")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @property
    def content_hash(self) -> str:
        encoded = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    def write_once(self, path: str | Path) -> Path:
        """Persist a preregistration without permitting silent mutation."""
        destination = Path(path)
        payload = {**self.to_dict(), "content_hash": self.content_hash}
        serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        if destination.exists():
            existing = json.loads(destination.read_text(encoding="utf-8"))
            if existing.get("content_hash") != self.content_hash:
                raise FileExistsError(
                    "Research protocol already exists with different content; create a new version."
                )
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(serialized, encoding="utf-8")
        return destination


MOMENTUM_PROFILES = (
    MomentumProfile("short", 0.55, 0.25, 0.10, 0.10),
    MomentumProfile("current", 0.35, 0.35, 0.20, 0.10),
    MomentumProfile("balanced", 0.30, 0.30, 0.30, 0.10),
    MomentumProfile("long", 0.15, 0.25, 0.50, 0.10),
    MomentumProfile("no_low_vol", 0.40, 0.35, 0.25, 0.00),
)

REGIME_PROFILES = (
    RegimeProfile("defensive", 20.0, 25.0, -0.05, 0.75),
    RegimeProfile("baseline", 22.0, 28.0, -0.08, 0.50),
    RegimeProfile("slow", 25.0, 32.0, -0.10, 0.50),
)


def core_candidate_grid() -> tuple[CandidateParameters, ...]:
    candidates: list[CandidateParameters] = []
    for momentum in MOMENTUM_PROFILES:
        for top_n in (3, 4, 5):
            for target_vol in (0.08, 0.10, 0.12):
                for regime in REGIME_PROFILES:
                    label = (
                        f"mom-{momentum.name}__top-{top_n}__vol-{target_vol:.2f}"
                        f"__regime-{regime.name}"
                    )
                    candidates.append(
                        CandidateParameters(
                            label=label,
                            momentum=momentum,
                            top_n=top_n,
                            target_annual_vol=target_vol,
                            regime=regime,
                        )
                    )
    if len(candidates) != 135 or len({item.label for item in candidates}) != 135:
        raise AssertionError("Core candidate grid must be 135 unique candidates.")
    return tuple(candidates)


def build_protocol(
    *,
    protocol_version: str,
    code_commit: str,
    dataset_snapshot_id: int,
    universe_version: str,
) -> ResearchProtocol:
    if not code_commit or not universe_version or dataset_snapshot_id < 1:
        raise ValueError("Protocol requires code, dataset, and universe provenance.")
    return ResearchProtocol(
        protocol_version=protocol_version,
        code_commit=code_commit,
        dataset_snapshot_id=dataset_snapshot_id,
        universe_version=universe_version,
        benchmark_names=("BIL", "60_40_SPY_IEF", "SPY"),
        cost_scenarios_bps=(2.0, 7.0, 20.0),
        outer_test_months=12,
        minimum_outer_training_years=5,
        candidates=core_candidate_grid(),
    )


def parameter_neighbors(
    selected: CandidateParameters,
    candidates: Iterable[CandidateParameters],
) -> tuple[CandidateParameters, ...]:
    selected_flat = (
        selected.momentum.name,
        selected.top_n,
        selected.target_annual_vol,
        selected.regime.name,
    )
    result = []
    for candidate in candidates:
        flat = (
            candidate.momentum.name,
            candidate.top_n,
            candidate.target_annual_vol,
            candidate.regime.name,
        )
        if sum(left != right for left, right in zip(selected_flat, flat)) == 1:
            result.append(candidate)
    return tuple(result)


def apply_candidate(
    base: Config,
    candidate: CandidateParameters,
    *,
    cost_bps: float = 7.0,
) -> Config:
    return replace(
        base,
        rebalance_frequency="M",
        execution_lag_sessions=1,
        top_n=candidate.top_n,
        target_annual_vol=candidate.target_annual_vol,
        weight_mom_20=candidate.momentum.weight_mom_20,
        weight_mom_60=candidate.momentum.weight_mom_60,
        weight_mom_120=candidate.momentum.weight_mom_120,
        weight_low_vol=candidate.momentum.weight_low_vol,
        vix_high_threshold=candidate.regime.vix_high_threshold,
        vix_risk_off_threshold=candidate.regime.vix_risk_off_threshold,
        max_allowed_drawdown_from_200d=candidate.regime.max_drawdown_from_200d,
        risk_off_cash_weight=candidate.regime.risk_off_cash_weight,
        min_asset_weight=0.10,
        max_asset_weight=0.35,
        risk_model="sample",
        trading_cost_bps=float(cost_bps),
        slippage_bps=0.0,
    )
